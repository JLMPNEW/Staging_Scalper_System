#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_TEMP_ROOT = Path("C:/tmp") if Path("C:/tmp").exists() else PROJECT_ROOT
PYTEST_TEMP_DIR = Path(os.environ.get("BIOTECH_PYTEST_TMP", str(DEFAULT_TEMP_ROOT / "biotech_pytest_tmp")))


@dataclass(frozen=True)
class GateStep:
    name: str
    command: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run biotech-only static checks and regression tests.")
    parser.add_argument("--skip-ruff", action="store_true", help="Skip ruff linting.")
    parser.add_argument(
        "--pyright",
        action="store_true",
        help="Also run the current biotech Pyright baseline. This is opt-in until the existing type backlog is resolved.",
    )
    parser.add_argument("--skip-pytest", action="store_true", help="Skip pytest regression tests.")
    return parser.parse_args()


def require_executable(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(
            f"Required executable not found: {name}. "
            "Run through uv with the needed tools, for example: "
            "uv run --with pytest --with ruff --with pyright --with pyyaml --with pandas "
            "--with requests --with ib-insync --with yfinance "
            "python biotech_index/scripts/00_run_biotech_quality_gate.py"
        )


def run_step(step: GateStep) -> None:
    start = time.monotonic()
    print(f"==> {step.name}", flush=True)
    completed = subprocess.run(step.command, cwd=PROJECT_ROOT)
    elapsed = time.monotonic() - start
    if completed.returncode != 0:
        raise SystemExit(f"{step.name} failed with exit code {completed.returncode} after {elapsed:.2f}s")
    print(f"<== {step.name} passed in {elapsed:.2f}s", flush=True)


def main() -> None:
    args = parse_args()
    steps = [
        GateStep("compileall", [sys.executable, "-m", "compileall", "-q", "biotech_index", "tests/biotech"]),
    ]
    if not args.skip_pytest:
        steps.append(
            GateStep(
                "pytest",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/biotech",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(PYTEST_TEMP_DIR),
                ],
            )
        )
    if not args.skip_ruff:
        require_executable("ruff")
        steps.append(GateStep("ruff", ["ruff", "check", "--config", "ruff.biotech.toml", "biotech_index", "tests/biotech"]))
    if args.pyright:
        require_executable("pyright")
        steps.append(GateStep("pyright", ["pyright", "--project", "pyrightconfig.biotech.json"]))

    for step in steps:
        run_step(step)


if __name__ == "__main__":
    main()
