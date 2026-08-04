#!/usr/bin/env python3
"""Fail-closed scheduled entry for the cross-sector nightly pipeline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
MASTER_RUNNER = ORCH_DIR / "run_all.py"
LATE_STATEMENT_RECONCILER = ORCH_DIR / "reconcile_late_ib_statements.py"
PROVIDER_VALIDATOR = (
    PROJECT_ROOT / "portfolio_layer" / "provider_ingestion" / "validate.py"
)
DEFAULT_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
RUNS_ROOT = ORCH_DIR / "runs"
NIGHTLY_ROOT = ORCH_DIR / "nightly_runs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled nightly portfolio refresh.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default="", help="Optional explicit target session.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _run_logged(command: list[str], log: TextIO) -> int:
    rendered = subprocess.list2cmdline(command)
    log.write(f"\n=== {rendered}\n")
    log.flush()
    print(rendered, flush=True)
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        log.write(line)
        log.flush()
    return int(process.wait())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _master_manifests() -> set[Path]:
    if not RUNS_ROOT.is_dir():
        return set()
    return set(RUNS_ROOT.glob("*/master_manifest.json"))


def _new_master_manifest(before: set[Path]) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = sorted(
        _master_manifests() - before,
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        return None, None
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, None
    return path, payload if isinstance(payload, dict) else None


def _selftest() -> int:
    root = Path(tempfile.mkdtemp())
    path = root / "manifest.json"
    _atomic_json(path, {"acceptance": "PASS", "value": 1})
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == {"acceptance": "PASS", "value": 1}
    print("NIGHTLY SELFTEST PASS: 1 check")
    return 0


def main() -> int:
    args = parse_args()
    if args.selftest:
        return _selftest()

    config = args.config.expanduser().resolve()
    if not config.is_file():
        raise FileNotFoundError(f"Config not found: {config}")

    started = datetime.now(timezone.utc)
    run_stamp = started.strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = NIGHTLY_ROOT / run_stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "nightly.log"
    manifest_path = run_dir / "nightly_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "nightly_orchestration_manifest_v2",
        "acceptance": "RUNNING",
        "started_at_utc": started.isoformat(timespec="seconds"),
        "requested_as_of": args.as_of,
        "dry_run": bool(args.dry_run),
        "provider_validation_rc": None,
        "late_statement_reconciliation_rc": None,
        "late_statement_reconciliation_manifest": "",
        "late_statement_reconciliation_acceptance": "",
        "master_rc": None,
        "master_manifest": "",
        "master_acceptance": "",
        "log_path": str(log_path),
    }
    _atomic_json(manifest_path, manifest)

    before = _master_manifests()
    with log_path.open("w", encoding="utf-8", newline="") as log:
        if args.dry_run:
            provider_rc = 0
        else:
            provider_rc = _run_logged(
                [sys.executable, str(PROVIDER_VALIDATOR), "--config", str(config)],
                log,
            )
        manifest["provider_validation_rc"] = provider_rc
        _atomic_json(manifest_path, manifest)
        if provider_rc != 0:
            manifest["acceptance"] = "FAIL_PROVIDER_STORE"
            manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            _atomic_json(manifest_path, manifest)
            return 1

        reconciliation_manifest_path = run_dir / "late_statement_reconciliation_manifest.json"
        reconciliation_command = [
            sys.executable,
            str(LATE_STATEMENT_RECONCILER),
            "--config",
            str(config),
            "--manifest",
            str(reconciliation_manifest_path),
        ]
        if args.as_of:
            reconciliation_command.extend(["--target", args.as_of])
        if args.dry_run:
            reconciliation_command.append("--dry-run")
        reconciliation_rc = _run_logged(reconciliation_command, log)
        reconciliation_payload = _load_json(reconciliation_manifest_path)
        reconciliation_acceptance = str(reconciliation_payload.get("acceptance", ""))
        manifest.update(
            {
                "late_statement_reconciliation_rc": reconciliation_rc,
                "late_statement_reconciliation_manifest": str(reconciliation_manifest_path),
                "late_statement_reconciliation_acceptance": reconciliation_acceptance,
            }
        )
        _atomic_json(manifest_path, manifest)
        accepted_reconciliation = reconciliation_acceptance in {"PASS", "PASS_DRY_RUN"}
        if reconciliation_rc != 0 or not accepted_reconciliation:
            manifest["acceptance"] = "FAIL_LATE_STATEMENT_RECONCILIATION"
            manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            _atomic_json(manifest_path, manifest)
            return 1

        command = [sys.executable, str(MASTER_RUNNER), "--catch-up"]
        if args.as_of:
            command.extend(["--as-of", args.as_of])
        if args.dry_run:
            command.append("--dry-run")
        master_rc = _run_logged(command, log)

    master_path, master_payload = _new_master_manifest(before)
    master_acceptance = str((master_payload or {}).get("acceptance", ""))
    manifest.update(
        {
            "master_rc": master_rc,
            "master_manifest": "" if master_path is None else str(master_path),
            "master_acceptance": master_acceptance,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    if args.dry_run:
        passed = master_rc == 0
    else:
        passed = master_rc == 0 and master_path is not None and master_acceptance == "PASS"
    manifest["acceptance"] = "PASS" if passed else "FAIL_MASTER"
    _atomic_json(manifest_path, manifest)
    print(f"nightly_manifest: {manifest_path}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
