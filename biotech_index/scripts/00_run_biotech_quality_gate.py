#!/usr/bin/env python3
from __future__ import annotations

import argparse
import compileall
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
LOGGER = logging.getLogger("run_biotech_quality_gate")


@dataclass(frozen=True)
class GateStep:
    name: str
    command: list[str]


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".biotech_quality_gate_probe_{os.getpid()}"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError:
        return False
    return True


def resolve_pytest_temp_dir() -> Path:
    exact_override = str(os.environ.get("BIOTECH_PYTEST_TMP") or "").strip()
    if exact_override:
        exact_path = Path(exact_override).expanduser().resolve()
        if not _is_writable_directory(exact_path.parent):
            raise RuntimeError(f"BIOTECH_PYTEST_TMP parent is not writable: {exact_path.parent}")
        return exact_path

    root_override = str(os.environ.get("BIOTECH_QUALITY_GATE_TMP_ROOT") or "").strip()
    candidates = [Path(root_override).expanduser()] if root_override else []
    candidates.append(Path(tempfile.gettempdir()))
    if os.name == "nt":
        candidates.append(Path(os.environ.get("SYSTEMDRIVE", "C:")) / "tmp")
    candidates.append(PROJECT_ROOT / ".pytest_tmp")
    for candidate in candidates:
        root = candidate.resolve()
        if _is_writable_directory(root):
            return root / f"biotech_quality_gate_{os.getpid()}"
    raise RuntimeError("No writable pytest temporary directory is available for the biotech quality gate")


def compile_biotech_sources() -> None:
    exclude_re = re.compile(r".*[\\/]_tmp_.*\.py$")
    ok = True
    for rel_path in ("biotech_index", "tests/biotech"):
        ok = compileall.compile_dir(PROJECT_ROOT / rel_path, quiet=1, rx=exclude_re) and ok
    if not ok:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run biotech-only static checks and regression tests.")
    parser.add_argument("--skip-ruff", action="store_true", help="Skip ruff linting.")
    parser.add_argument(
        "--pyright",
        action="store_true",
        help="Also run the biotech Pyright gate.",
    )
    parser.add_argument("--skip-pytest", action="store_true", help="Skip pytest regression tests.")
    parser.add_argument(
        "--allow-empty-gate",
        action="store_true",
        help="Allow running with all substantive checks skipped (compileall only). Off by default because that passes vacuously.",
    )
    return parser.parse_args()


def run_step(step: GateStep) -> None:
    start = time.monotonic()
    print(f"==> {step.name}", flush=True)
    if step.command == ["__compile_biotech_sources__"]:
        compile_biotech_sources()
        returncode = 0
    else:
        completed = subprocess.run(step.command, cwd=PROJECT_ROOT)
        returncode = completed.returncode
    elapsed = time.monotonic() - start
    if returncode != 0:
        raise SystemExit(f"{step.name} failed with exit code {returncode} after {elapsed:.2f}s")
    print(f"<== {step.name} passed in {elapsed:.2f}s", flush=True)


def main() -> None:
    args = parse_args()
    if args.skip_pytest and args.skip_ruff and not args.pyright and not args.allow_empty_gate:
        raise SystemExit(
            "Refusing to run the quality gate with --skip-pytest and --skip-ruff together: "
            "compileall alone passes vacuously. Re-enable at least one substantive check "
            "(pytest, ruff, or --pyright) or pass --allow-empty-gate to override."
        )
    steps = [
        GateStep("compileall", ["__compile_biotech_sources__"]),
    ]
    if not args.skip_pytest:
        pytest_temp_dir = resolve_pytest_temp_dir()
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
                    str(pytest_temp_dir),
                ],
            )
        )
    if not args.skip_ruff:
        steps.append(
            GateStep(
                "ruff",
                [sys.executable, "-m", "ruff", "check", "--config", "ruff.biotech.toml", "biotech_index", "tests/biotech"],
            )
        )
    if args.pyright:
        steps.append(
            GateStep(
                "pyright",
                [sys.executable, "-m", "pyright", "--project", "pyrightconfig.biotech.json"],
            )
        )

    for step in steps:
        run_step(step)


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt, GeneratorExit):
        raise
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        LOGGER.exception("Fatal biotech quality gate error: %s", exc)
        raise SystemExit(1) from exc
