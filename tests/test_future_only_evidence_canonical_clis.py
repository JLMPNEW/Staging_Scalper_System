from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from industrials.transportation.future_oos_activation_v6 import (
    REQUIRED_PLAN_ROLES as TRANSPORT_PLAN_ROLES,
)
from industrials.transportation.future_oos_capture_v6 import (
    REQUIRED_CAPTURE_ROLES_V6,
)


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SCRIPTS = (
    ROOT / "industrials/transportation/scripts/45h_capture_transportation_future_oos.py",
    ROOT / "industrials/transportation/scripts/45i_evaluate_transportation_future_oos.py",
)
ZERO_HASH = "0" * 64
@pytest.mark.parametrize("script", CANONICAL_SCRIPTS)
def test_canonical_future_cli_help_is_available_without_mutation(script: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.casefold()
    assert "--authority-registry" not in completed.stdout
    assert "--trusted-public-key" not in completed.stdout


def _role_args(flag: str, roles: set[str] | frozenset[str]) -> list[str]:
    result: list[str] = []
    for role in sorted(roles):
        result.extend([flag, f"{role}=missing-{role}"])
    return result


def _source_hash_args(roles: set[str] | frozenset[str]) -> list[str]:
    result: list[str] = []
    for role in sorted(roles):
        result.extend(["--source-sha256", f"{role}={ZERO_HASH}"])
    return result


def _run_fails_at_unconfigured_trust(
    script: Path,
    args: list[str],
    output: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode != 0
    assert "unconfigured" in (completed.stdout + completed.stderr).casefold()
    assert not output.exists()


def test_transport_canonical_capture_is_executable_but_fail_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "transport-capture.json"
    args = [
        "--activation-plan",
        "missing-activation.json",
        *_role_args("--plan-source", TRANSPORT_PLAN_ROLES),
        "--activation-receipt",
        "missing-activation-receipt.json",
        "--activation-receipt-sha256",
        ZERO_HASH,
        "--activation-timestamp-receipt",
        "missing-activation-ts.json",
        "--activation-timestamp-receipt-sha256",
        ZERO_HASH,
        "--evidence-public-key",
        "missing-evidence.pem",
        "--timestamp-public-key",
        "missing-timestamp.pem",
        "--market-data-public-key",
        "missing-market.pem",
        "--asof",
        "2026-09-30",
        *_role_args("--source", REQUIRED_CAPTURE_ROLES_V6),
        *_source_hash_args(REQUIRED_CAPTURE_ROLES_V6),
        "--capture-receipt",
        "missing-capture-receipt.json",
        "--capture-receipt-sha256",
        ZERO_HASH,
        "--capture-timestamp-receipt",
        "missing-capture-ts.json",
        "--capture-timestamp-receipt-sha256",
        ZERO_HASH,
        "--archive-root",
        str(tmp_path / "archive"),
        "--output",
        str(output),
    ]
    _run_fails_at_unconfigured_trust(CANONICAL_SCRIPTS[0], args, output)


def test_transport_canonical_evaluator_is_executable_but_fail_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "transport-evaluation.json"
    args = [
        "--activation-plan",
        "missing-activation.json",
        *_role_args("--plan-source", TRANSPORT_PLAN_ROLES),
        "--activation-receipt",
        "missing-activation-receipt.json",
        "--activation-receipt-sha256",
        ZERO_HASH,
        "--activation-timestamp-receipt",
        "missing-activation-ts.json",
        "--activation-timestamp-receipt-sha256",
        ZERO_HASH,
        "--evidence-public-key",
        "missing-evidence.pem",
        "--timestamp-public-key",
        "missing-timestamp.pem",
        "--market-data-public-key",
        "missing-market.pem",
        "--capture",
        "missing-capture.json",
        "--capture-registry",
        "missing-registry.json",
        "--capture-registry-receipt",
        "missing-registry-receipt.json",
        "--capture-registry-receipt-sha256",
        ZERO_HASH,
        "--capture-registry-timestamp-receipt",
        "missing-registry-ts.json",
        "--capture-registry-timestamp-receipt-sha256",
        ZERO_HASH,
        "--outcomes",
        "missing-outcomes.json",
        "--outcome-source",
        "total_return_bars=missing-bars.csv",
        "--outcome-receipt",
        "missing-outcome-receipt.json",
        "--outcome-receipt-sha256",
        ZERO_HASH,
        "--outcome-timestamp-receipt",
        "missing-outcome-ts.json",
        "--outcome-timestamp-receipt-sha256",
        ZERO_HASH,
        "--market-export-receipt",
        "missing-market-export.json",
        "--market-export-receipt-sha256",
        ZERO_HASH,
        "--trading-calendar",
        "missing-calendar.csv",
        "--output",
        str(output),
    ]
    _run_fails_at_unconfigured_trust(CANONICAL_SCRIPTS[1], args, output)


def test_canonical_cli_sources_do_not_import_superseded_authority_routes() -> None:
    for script in CANONICAL_SCRIPTS:
        source = script.read_text(encoding="utf-8")
        assert "authority_config" not in source
        assert "DEFAULT_AUTHORITY_REGISTRY" not in source
        assert "BLOCKED_FAIL_CLOSED" not in source
    assert "future_oos_capture_v6" in CANONICAL_SCRIPTS[0].read_text(encoding="utf-8")
    assert "future_oos_protocol_v6" in CANONICAL_SCRIPTS[1].read_text(encoding="utf-8")
