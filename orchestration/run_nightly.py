#!/usr/bin/env python3
"""Fail-closed scheduled entry for the cross-sector nightly pipeline.

Policy (2026-08-07): a stale/broken HISTORICAL input must never prevent CURRENT
production. The late-IB-statement reconciler still runs first and its failure
still fails the nightly's acceptance (FAIL_LATE_STATEMENT_RECONCILIATION, loud),
but it no longer blocks the master run -- sector refreshes and the current
portfolio are always attempted. --mode daily provides a scheduled plain-daily
lane (no catch-up scanning) as an operator escape hatch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

import run_all as run_all_mod


ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
MASTER_RUNNER = ORCH_DIR / "run_all.py"
LATE_STATEMENT_RECONCILER = ORCH_DIR / "reconcile_late_ib_statements.py"
PROVIDER_VALIDATOR = PROJECT_ROOT / "portfolio_layer" / "provider_ingestion" / "validate.py"
DEFAULT_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
RUNS_ROOT = ORCH_DIR / "runs"
NIGHTLY_ROOT = ORCH_DIR / "nightly_runs"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.runtime_env import (  # noqa: E402
    hydrate_missing_user_environment,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled nightly portfolio refresh.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default="", help="Optional explicit target session.")
    parser.add_argument(
        "--mode",
        choices=["catch-up", "daily"],
        default="catch-up",
        help="Master mode: catch-up (default; current-target-first with best-effort "
        "backfill) or daily (current session only, no gap scanning).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _is_first_trading_session_of_week(iso_date: str) -> bool:
    target = run_all_mod._to_date(iso_date)
    week = target.isocalendar()[:2]
    cursor = target - timedelta(days=1)
    while cursor.isocalendar()[:2] == week:
        if run_all_mod.is_trading_day(cursor):
            return False
        cursor -= timedelta(days=1)
    return True


def master_command(mode: str, as_of: str, dry_run: bool, *, cadence: str = "daily") -> list[str]:
    """The run_all invocation for this nightly. catch-up is CURRENT-TARGET-FIRST in
    run_all (an unbuildable historical date can no longer wedge the current book);
    daily is the plain single-session lane."""
    command = [sys.executable, str(MASTER_RUNNER)]
    if mode == "catch-up":
        command.append("--catch-up")
    if as_of:
        command.extend(["--as-of", as_of])
    if cadence == "weekly":
        command.extend(["--cadence", "weekly"])
    if dry_run:
        command.append("--dry-run")
    return command


def reconcile_abandoned_nightly_runs(root: Path = NIGHTLY_ROOT) -> list[Path]:
    """Fail-close nightly manifests stuck at RUNNING whose recorded process is gone.

    A killed/crashed nightly bypasses main()'s tail and would otherwise read
    acceptance=RUNNING as a terminal state forever (master manifests are reconciled
    by run_all; nightly manifests had no equivalent). Only a manifest that recorded
    its nightly_pid AND whose pid is provably dead (PID-identity aware via the
    recorded start time) is amended.
    """
    reconciled: list[Path] = []
    if not root.is_dir():
        return reconciled
    for manifest_path in sorted(root.glob("*/nightly_manifest.json")):
        payload = _load_json(manifest_path)
        if str(payload.get("acceptance") or "").upper() != "RUNNING":
            continue
        pid_raw = payload.get("nightly_pid")
        if not isinstance(pid_raw, int) or pid_raw <= 0:
            continue  # pre-v3 manifest without a pid: cannot verify, leave as-is
        started_iso = str(payload.get("started_at_utc") or "")
        if run_all_mod._holder_alive(pid_raw, started_iso) is not False:
            continue
        payload["acceptance"] = "ABORTED"
        payload["aborted_reason"] = f"nightly process pid={pid_raw} is no longer alive; run did not seal"
        payload["reconciled_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_json(manifest_path, payload)
        reconciled.append(manifest_path)
    return reconciled


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
    checks = 0
    root = Path(tempfile.mkdtemp())
    path = root / "manifest.json"
    _atomic_json(path, {"acceptance": "PASS", "value": 1})
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == {"acceptance": "PASS", "value": 1}
    checks += 1

    # master command construction: catch-up default, daily escape lane
    cmd_cu = master_command("catch-up", "", False)
    assert "--catch-up" in cmd_cu and "--as-of" not in cmd_cu
    cmd_daily = master_command("daily", "2026-08-06", True)
    assert "--catch-up" not in cmd_daily
    assert cmd_daily[-3:] == ["--as-of", "2026-08-06", "--dry-run"]
    cmd_weekly = master_command("catch-up", "2026-08-17", True, cadence="weekly")
    assert cmd_weekly[-5:] == ["--as-of", "2026-08-17", "--cadence", "weekly", "--dry-run"]
    assert _is_first_trading_session_of_week("2026-08-17")
    assert not _is_first_trading_session_of_week("2026-08-18")
    assert _is_first_trading_session_of_week("2026-09-08")  # Labor Day Monday
    checks += 1

    # abandoned-nightly reconciliation: dead pid -> ABORTED; live/unknown pid kept
    nightly_root = root / "nightly_runs"
    dead_dir = nightly_root / "dead"
    dead_dir.mkdir(parents=True)
    _atomic_json(
        dead_dir / "nightly_manifest.json",
        {"acceptance": "RUNNING", "nightly_pid": 2_000_000_000, "started_at_utc": "2020-01-01T00:00:00+00:00"},
    )
    live_dir = nightly_root / "live"
    live_dir.mkdir(parents=True)
    _atomic_json(
        live_dir / "nightly_manifest.json",
        {
            "acceptance": "RUNNING",
            "nightly_pid": os.getpid(),
            "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    nopid_dir = nightly_root / "nopid"
    nopid_dir.mkdir(parents=True)
    _atomic_json(nopid_dir / "nightly_manifest.json", {"acceptance": "RUNNING"})
    sealed_dir = nightly_root / "sealed"
    sealed_dir.mkdir(parents=True)
    _atomic_json(sealed_dir / "nightly_manifest.json", {"acceptance": "PASS", "nightly_pid": 1})
    reconciled = reconcile_abandoned_nightly_runs(nightly_root)
    assert [p.parent.name for p in reconciled] == ["dead"], reconciled
    dead_payload = _load_json(dead_dir / "nightly_manifest.json")
    assert dead_payload["acceptance"] == "ABORTED" and "aborted_reason" in dead_payload
    assert _load_json(live_dir / "nightly_manifest.json")["acceptance"] == "RUNNING"
    assert _load_json(nopid_dir / "nightly_manifest.json")["acceptance"] == "RUNNING"
    assert _load_json(sealed_dir / "nightly_manifest.json")["acceptance"] == "PASS"
    checks += 1

    # nightly acceptance resolution: reconciliation failure is loud but never blocks
    assert _resolve_acceptance(reconciliation_failed=False, passed=True) == "PASS"
    assert _resolve_acceptance(reconciliation_failed=False, passed=False) == "FAIL_MASTER"
    assert _resolve_acceptance(reconciliation_failed=True, passed=True) == "FAIL_LATE_STATEMENT_RECONCILIATION"
    assert _resolve_acceptance(reconciliation_failed=True, passed=False) == "FAIL_LATE_STATEMENT_RECONCILIATION"
    checks += 1

    print(f"NIGHTLY SELFTEST PASS: {checks} checks")
    return 0


def _resolve_acceptance(*, reconciliation_failed: bool, passed: bool) -> str:
    """Final nightly acceptance. A reconciliation failure stays a FAILING verdict
    (fail-closed reporting) but -- by policy -- the master has still run, so the
    current book is produced either way."""
    if reconciliation_failed:
        return "FAIL_LATE_STATEMENT_RECONCILIATION"
    return "PASS" if passed else "FAIL_MASTER"


def main() -> int:
    args = parse_args()
    if args.selftest:
        return _selftest()

    hydrated = hydrate_missing_user_environment()
    if hydrated:
        print(
            "Hydrated missing local user environment variables by name: " + ", ".join(hydrated),
            flush=True,
        )

    config = args.config.expanduser().resolve()
    if not config.is_file():
        raise FileNotFoundError(f"Config not found: {config}")

    # Resolve the target exactly once so the reconciler and master cannot disagree
    # across a session boundary. Loading the real registry also installs ad-hoc
    # market closures before the first-session-of-week test.
    run_all_mod.load_registry(run_all_mod.DEFAULT_REGISTRY)
    requested_as_of = args.as_of
    if requested_as_of:
        requested_as_of = run_all_mod.parse_iso(requested_as_of)
        if not run_all_mod.is_trading_day(run_all_mod._to_date(requested_as_of)):
            raise SystemExit(f"--as-of {requested_as_of} is not a trading session (weekend/holiday/closure)")
    effective_as_of = run_all_mod.resolve_target_date(requested_as_of)
    cadence = "weekly" if _is_first_trading_session_of_week(effective_as_of) else "daily"

    for reconciled in reconcile_abandoned_nightly_runs():
        print(f"reconciled abandoned RUNNING nightly manifest -> ABORTED: {reconciled}", flush=True)

    started = datetime.now(timezone.utc)
    run_stamp = started.strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = NIGHTLY_ROOT / run_stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "nightly.log"
    manifest_path = run_dir / "nightly_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "nightly_orchestration_manifest_v3",
        "acceptance": "RUNNING",
        "started_at_utc": started.isoformat(timespec="seconds"),
        "nightly_pid": os.getpid(),
        "requested_as_of": requested_as_of,
        "effective_as_of": effective_as_of,
        "cadence": cadence,
        "mode": args.mode,
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
            manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
        reconciliation_command.extend(["--target", effective_as_of])
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
        reconciliation_failed = reconciliation_rc != 0 or not accepted_reconciliation
        if reconciliation_failed:
            # POLICY (2026-08-07): a broken/unparsable HISTORICAL statement must not
            # block current production. The failure stays a failing nightly verdict
            # (FAIL_LATE_STATEMENT_RECONCILIATION below) so it cannot be overlooked,
            # but the master still runs and the current book is still produced.
            log.write(
                "\n=== late-statement reconciliation FAILED; continuing to the master "
                "run per current-production policy (nightly acceptance will FAIL)\n"
            )
            log.flush()
            print(
                "late-statement reconciliation FAILED; continuing to master run "
                "(nightly acceptance will be FAIL_LATE_STATEMENT_RECONCILIATION)",
                flush=True,
            )

        master_rc = _run_logged(
            master_command(args.mode, effective_as_of, args.dry_run, cadence=cadence),
            log,
        )

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
    manifest["acceptance"] = _resolve_acceptance(reconciliation_failed=reconciliation_failed, passed=passed)
    _atomic_json(manifest_path, manifest)
    print(f"nightly_manifest: {manifest_path}", flush=True)
    return 0 if manifest["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
