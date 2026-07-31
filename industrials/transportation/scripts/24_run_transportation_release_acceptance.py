#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.release_contract import (  # noqa: E402
    DEFAULT_RELEASE_NAME,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)

MODEL_FAMILY = "transportation"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and persist the final transportation code-aligned release "
            "acceptance suite. This performs no retrieval, parsing, feature "
            "build, historical materialization, calibration, or promotion."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
    parser.add_argument("--release-dir", type=Path, default=None)
    return parser.parse_args()


def acceptance_commands() -> list[tuple[str, list[str]]]:
    industrial_tests = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests" / "industrials").glob("test_*.py")
    )
    parser_tests = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests" / "dedicated_parser").glob("test_*.py")
    )
    transportation_tests = sorted(
        path
        for path in industrial_tests
        if Path(path).name.startswith("test_transportation")
    )
    return [
        (
            "full_industrials_and_dedicated_parser_tests",
            [
                sys.executable,
                "-m",
                "pytest",
                *industrial_tests,
                *parser_tests,
                "-q",
            ],
        ),
        (
            "transportation_ruff",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "industrials/transportation",
                *transportation_tests,
            ],
        ),
        (
            "transportation_pyright",
            [
                sys.executable,
                "-m",
                "pyright",
                "industrials/transportation",
            ],
        ),
        (
            "transportation_compile",
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "industrials/transportation",
            ],
        ),
    ]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_name = str(args.release_name).strip()
    release_dir = (
        args.release_dir.expanduser().resolve()
        if args.release_dir
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "releases"
        / asof
        / release_name
    )
    acceptance_dir = release_dir / "acceptance"
    integrity_path = release_dir / "transportation_release_integrity_audit.json"
    integrity_command = [
        sys.executable,
        "industrials/transportation/scripts/23_audit_transportation_release_integrity.py",
        "--asof",
        asof,
        "--release-name",
        release_name,
        "--release-dir",
        str(release_dir),
    ]
    commands = [("release_integrity", integrity_command), *acceptance_commands()]
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for gate_id, command in commands:
        completed = run_command(command)
        log_path = acceptance_dir / f"{gate_id}.log"
        log_body = (
            f"command={json.dumps(command)}\n"
            f"returncode={completed.returncode}\n"
            "stdout:\n"
            f"{completed.stdout}\n"
            "stderr:\n"
            f"{completed.stderr}\n"
        )
        write_text_atomic(log_path, log_body)
        passed = completed.returncode == 0
        if not passed:
            failures.append(gate_id)
        results.append(
            {
                "gate_id": gate_id,
                "status": "PASS" if passed else "FAIL",
                "returncode": completed.returncode,
                "command": command,
                "log": artifact(log_path),
            }
        )
    integrity = read_json(integrity_path) if integrity_path.is_file() else {}
    if integrity.get("acceptance") != "PASS":
        failures.append("release_integrity_payload")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    python_files = list(
        (PROJECT_ROOT / "industrials" / "transportation").rglob("*.py")
    )
    acceptance = "PASS" if not failures else "FAIL"
    manifest_path = (
        release_dir / "transportation_release_acceptance_manifest.json"
    )
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_FINAL_RELEASE_ACCEPTANCE",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "release_name": release_name,
        "release_dir": str(release_dir),
        "git_commit_sha": head,
        "gate_count": len(results),
        "passed_gate_count": sum(
            result["status"] == "PASS" for result in results
        ),
        "failed_gate_count": len(set(failures)),
        "failed_gates": sorted(set(failures)),
        "transportation_python_file_count": len(python_files),
        "release_integrity": (
            artifact(integrity_path) if integrity_path.is_file() else {}
        ),
        "results": results,
        "implementation_status": (
            "COMPLETE_SHADOW_NOT_PROMOTED"
            if acceptance == "PASS"
            else "RELEASE_ACCEPTANCE_FAILED"
        ),
        "production_model_promoted": False,
        "portfolio_integration_mode": "fail_closed_shadow",
        "operations": {
            "network_requests": 0,
            "parser_invocations": 0,
            "feature_rebuilds": 0,
            "historical_materializations": 0,
            "calibration_invocations": 0,
            "database_writes": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "errors": sorted(set(failures)),
        "next_gate": (
            "IMPLEMENTATION_COMPLETE_CONTINUE_OUTCOME_BLIND_MONITORING"
            if acceptance == "PASS"
            else "REPAIR_RELEASE_ACCEPTANCE_FAILURES"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())