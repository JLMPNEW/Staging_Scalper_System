#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the canonical SEC Form 4 source path for med-devices: "
            "SEC_FORM4_Runner -> sec_insider.sqlite -> med_devices import/features/audit."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="")
    parser.add_argument("--history-start", default="")
    parser.add_argument("--runner-config", type=Path, default=None)
    parser.add_argument("--runner-profile", default="")
    parser.add_argument("--runner-timeout-sec", type=int, default=None)
    parser.add_argument("--runner-heartbeat-sec", type=int, default=None)
    parser.add_argument(
        "--fail-on-runner-timeout",
        action="store_true",
        help="Fail instead of importing/building from the last completed canonical Form 4 DB if the live runner times out.",
    )
    parser.add_argument("--skip-runner", action="store_true", help="Skip SEC_FORM4_Runner and only import/build/audit.")
    parser.add_argument("--skip-import", action="store_true", help="Skip med-devices import from sec_insider.sqlite.")
    parser.add_argument("--skip-feature-build", action="store_true", help="Skip script 60 insider feature build.")
    parser.add_argument("--skip-coverage-audit", action="store_true", help="Skip external positioning coverage audit.")
    parser.add_argument("--skip-missing-ticker-audit", action="store_true", help="Skip detailed Form 4 missing-ticker audit.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    return parser.parse_args()


def terminate_process_tree(proc: subprocess.Popen[object]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run_command(
    command: list[str],
    *,
    dry_run: bool,
    timeout_sec: int | None = None,
    heartbeat_sec: int | None = None,
    allow_timeout: bool = False,
) -> bool:
    print(" ".join(command))
    if dry_run:
        return True
    started = perf_counter()
    next_heartbeat = float(heartbeat_sec or 0)
    proc = subprocess.Popen(command)
    while True:
        return_code = proc.poll()
        elapsed = perf_counter() - started
        if return_code is not None:
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
            print(f"completed command elapsed_sec={elapsed:.1f}")
            return True
        if timeout_sec is not None and elapsed >= timeout_sec:
            print(f"timeout command elapsed_sec={elapsed:.1f}; terminating process tree")
            terminate_process_tree(proc)
            if allow_timeout:
                return False
            raise subprocess.TimeoutExpired(command, timeout_sec)
        if heartbeat_sec and elapsed >= next_heartbeat:
            print(f"still_running elapsed_sec={elapsed:.1f} timeout_sec={timeout_sec}")
            next_heartbeat += heartbeat_sec
        sleep(1.0)


def resolved_script(relative: str) -> Path:
    path = PROJECT_ROOT / relative
    if not path.exists():
        raise FileNotFoundError(f"Required script not found: {path}")
    return path


def write_status(path: Path, payload: dict[str, Any], *, dry_run: bool) -> None:
    print(f"status_json={path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = args.asof.strip() or date.today().isoformat()
    history_start = args.history_start.strip() or str(cfg_get(config, "external_positioning_import.history_start", "2019-01-01"))
    runner_config = (
        args.runner_config.expanduser().resolve()
        if args.runner_config
        else resolve_path(
            cfg_get(
                config,
                "external_positioning_import.sec_form4_runner_config_path",
                "../SEC_FORM4_Runner/config_sec_form4_orchestrator_staging.yaml",
            ),
            base_dir=base_dir,
        )
    )
    runner_profile = args.runner_profile.strip() or str(
        cfg_get(config, "external_positioning_import.sec_form4_runner_profile", "weekly")
    )
    timeout_sec = int(
        args.runner_timeout_sec
        if args.runner_timeout_sec is not None
        else cfg_get(config, "external_positioning_import.sec_form4_runner_timeout_sec", 14400)
    )
    heartbeat_sec = int(
        args.runner_heartbeat_sec
        if args.runner_heartbeat_sec is not None
        else cfg_get(config, "external_positioning_import.sec_form4_runner_heartbeat_sec", 60)
    )
    allow_timeout_fallback = (not args.fail_on_runner_timeout) and bool(
        cfg_get(config, "external_positioning_import.sec_form4_runner_timeout_fallback", True)
    )
    status_json = resolve_path(
        cfg_get(
            config,
            "external_positioning_import.sec_form4_status_json",
            "../output/med_devices_reports/med_device_form4_refresh_status.json",
        ),
        base_dir=base_dir,
    )
    status: dict[str, Any] = {
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": asof,
        "history_start": history_start,
        "runner_profile": runner_profile,
        "runner_timeout_sec": timeout_sec,
        "runner_heartbeat_sec": heartbeat_sec,
        "runner_timeout_fallback": allow_timeout_fallback,
        "runner_completed": None,
        "runner_status": "skipped" if args.skip_runner else "pending",
        "import_completed": False,
        "feature_build_completed": False,
        "coverage_audit_completed": False,
        "missing_ticker_audit_completed": False,
    }

    if not args.skip_runner:
        runner_completed = run_command(
            [
                sys.executable,
                str(resolved_script("SEC_FORM4_Runner/run_sec_form4_orchestrator.py")),
                "--config",
                str(runner_config),
                "--target",
                "form4",
                "--profile",
                runner_profile,
                "--as-of-date",
                asof,
            ],
            dry_run=args.dry_run,
            timeout_sec=timeout_sec,
            heartbeat_sec=heartbeat_sec,
            allow_timeout=allow_timeout_fallback,
        )
        status["runner_completed"] = runner_completed
        status["runner_status"] = "completed" if runner_completed else "timed_out_fallback_to_existing_canonical_db"
        if not runner_completed:
            print(
                "WARNING: SEC_FORM4_Runner timed out. Continuing with import/features/audit "
                "from the last completed canonical sec_insider.sqlite state."
            )
    else:
        status["runner_completed"] = False
    if not args.skip_import:
        run_command(
            [
                sys.executable,
                str(resolved_script("med_devices/scripts/61_import_med_device_external_positioning_facts.py")),
                "--config",
                str(config_path),
                "--history-start",
                history_start,
                "--asof",
                asof,
                "--sources",
                "form4",
            ],
            dry_run=args.dry_run,
        )
        status["import_completed"] = True
    if not args.skip_feature_build:
        run_command(
            [
                sys.executable,
                str(resolved_script("med_devices/scripts/60_build_med_device_insider_activity_features.py")),
                "--config",
                str(config_path),
                "--asof",
                asof,
            ],
            dry_run=args.dry_run,
        )
        status["feature_build_completed"] = True
    if not args.skip_coverage_audit:
        run_command(
            [
                sys.executable,
                str(resolved_script("med_devices/scripts/67_audit_med_device_external_positioning_coverage.py")),
                "--config",
                str(config_path),
                "--asof",
                asof,
            ],
            dry_run=args.dry_run,
        )
        status["coverage_audit_completed"] = True
    if not args.skip_missing_ticker_audit:
        run_command(
            [
                sys.executable,
                str(resolved_script("med_devices/scripts/69_audit_med_device_form4_missing_tickers.py")),
                "--config",
                str(config_path),
                "--history-start",
                history_start,
                "--asof",
                asof,
            ],
            dry_run=args.dry_run,
        )
        status["missing_ticker_audit_completed"] = True
    status["completed_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_status(status_json, status, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
