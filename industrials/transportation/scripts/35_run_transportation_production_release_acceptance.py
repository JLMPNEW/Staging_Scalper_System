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
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    block_legacy_route,
)

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.selected_feature_history import sha256  # noqa: E402


MODEL_FAMILY = "transportation"
DEFAULT_RELEASE_NAME = "generic_oos_production_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and persist final transportation production-release "
            "acceptance without retrieval, parsing, rebuilding, calibration, "
            "promotion, portfolio writes, or configuration writes."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
    parser.add_argument("--release-dir", type=Path, default=None)
    return parser.parse_args()


def acceptance_commands(
    *,
    asof: str,
    release_name: str,
    release_dir: Path,
) -> list[tuple[str, list[str]]]:
    industrial_tests = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests" / "industrials").glob("test_*.py")
    )
    parser_tests = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests" / "dedicated_parser").glob(
            "test_*.py"
        )
    )
    transportation_tests = [
        path
        for path in industrial_tests
        if Path(path).name.startswith("test_transportation")
    ]
    return [
        (
            "production_release_integrity",
            [
                sys.executable,
                "industrials/transportation/scripts/"
                "34_audit_transportation_production_release_integrity.py",
                "--asof",
                asof,
                "--release-name",
                release_name,
                "--release-dir",
                str(release_dir),
            ],
        ),
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


def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
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
    block_legacy_route("35_run_transportation_production_release_acceptance")
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
    if not release_dir.is_dir():
        raise FileNotFoundError(release_dir)
    acceptance_dir = release_dir / "acceptance"
    acceptance_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for gate_id, command in acceptance_commands(
        asof=asof,
        release_name=release_name,
        release_dir=release_dir,
    ):
        completed = run(command)
        log_path = acceptance_dir / (gate_id + ".log")
        write_text_atomic(
            log_path,
            "command={}\nreturncode={}\nstdout:\n{}\nstderr:\n{}\n".format(
                json.dumps(command),
                completed.returncode,
                completed.stdout,
                completed.stderr,
            ),
        )
        status = "PASS" if completed.returncode == 0 else "FAIL"
        if status == "FAIL":
            failures.append(gate_id)
        results.append(
            {
                "gate_id": gate_id,
                "status": status,
                "returncode": completed.returncode,
                "command": command,
                "log": artifact(log_path),
            }
        )

    payload = {
        "acceptance": "PASS" if not failures else "FAIL",
        "gate": "TRANSPORTATION_PRODUCTION_RELEASE_ACCEPTANCE",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "release_name": release_name,
        "release_dir": str(release_dir),
        "gate_count": len(results),
        "passed_gate_count": sum(
            item["status"] == "PASS" for item in results
        ),
        "failed_gate_count": len(failures),
        "failed_gates": failures,
        "production_model_promoted": True,
        "portfolio_integration_mode": "production_generic_oos",
        "production_allocation_authorized": not failures,
        "results": results,
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
        "errors": failures,
    }
    manifest_path = (
        release_dir
        / "transportation_production_release_acceptance_manifest.json"
    )
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
