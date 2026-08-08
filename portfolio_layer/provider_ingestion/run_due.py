#!/usr/bin/env python3
"""Dispatch at most one current provider capture that is due under the ET schedule.

Every non-dry-run invocation (fresh capture, already-complete, and no-capture-due
status checks) emits a verified ingestion summary with actual completion times,
per-cycle coverage, output-hash verification, and provider-store validation. The
summary is printed to stdout and appended to ``run_due_summary.log`` next to the
capture artifacts because the Windows scheduled task discards stdout. Validation
failures and missed scheduled phases exit nonzero with no success banner.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.store import connect_store, verify_store  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CAPTURE_SCRIPT = Path(__file__).with_name("capture.py")
SUMMARY_LOG_NAME = "run_due_summary.log"
SUMMARY_LATEST_NAME = "run_due_summary_latest.txt"
PHASE_LABELS = {
    "sunday_baseline": "Sunday baseline",
    "premarket": "Premarket",
    "priority_refresh": "Priority refresh",
    "postclose": "Postclose",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--now-utc", type=datetime.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _minutes(value: str) -> int:
    hour, minute = (int(token) for token in value.split(":"))
    parsed = time(hour, minute)
    return parsed.hour * 60 + parsed.minute


def _phases_for_weekday(weekday: int) -> tuple[str, ...]:
    if weekday == 6:
        return ("sunday_baseline",)
    if weekday < 5:
        return ("premarket", "priority_refresh", "postclose")
    return ()


def due_phase(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
    grace_minutes: int,
) -> str | None:
    current = now_local.hour * 60 + now_local.minute
    candidates: list[tuple[int, str]] = []
    for phase in _phases_for_weekday(now_local.weekday()):
        configured = str(schedules.get(phase, "disabled")).strip().casefold()
        if configured == "disabled":
            continue
        lag = current - _minutes(configured)
        if 0 <= lag <= grace_minutes:
            candidates.append((lag, phase))
    return None if not candidates else min(candidates)[1]


def scheduled_phase_progress(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (expected phases today, phases whose scheduled time has passed)."""
    current = now_local.hour * 60 + now_local.minute
    expected: list[str] = []
    required: list[str] = []
    for phase in _phases_for_weekday(now_local.weekday()):
        configured = str(schedules.get(phase, "disabled")).strip().casefold()
        if configured == "disabled":
            continue
        expected.append(phase)
        if current >= _minutes(configured):
            required.append(phase)
    return tuple(expected), tuple(required)


def _format_capture_time(value: str, zone: ZoneInfo) -> str:
    completed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if completed.tzinfo is None:
        raise ValueError(f"Capture completion timestamp is timezone-naive: {value}")
    local = (completed.astimezone(zone) + timedelta(seconds=30)).replace(
        second=0, microsecond=0
    )
    hour = local.strftime("%I").lstrip("0") or "0"
    zone_label = "ET" if zone.key == "America/New_York" else zone.key
    return f"{hour}:{local:%M %p} {zone_label}"


def _successful_phase_rows(
    conn: Any,
    *,
    actual_capture_date: str,
    phases: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for phase in phases:
        row = conn.execute(
            "SELECT cr.*,cu.member_count FROM capture_runs cr "
            "JOIN capture_universes cu ON cu.universe_id=cr.universe_id "
            "WHERE cr.actual_capture_date=? AND cr.capture_phase=? "
            "AND cr.cycle_id LIKE 'scheduled-%' "
            "AND cr.status IN ('PASS','PASS_WITH_WARNINGS') "
            "ORDER BY cr.rowid DESC LIMIT 1",
            (actual_capture_date, phase),
        ).fetchone()
        if row is not None:
            rows[phase] = dict(row)
    return rows


def _capture_manifest_errors(
    row: Mapping[str, Any],
    *,
    output_root: Path,
) -> list[str]:
    cycle_id = str(row["cycle_id"])
    cycle_dir = (output_root / cycle_id).resolve()
    manifest_path = cycle_dir / "capture_manifest.json"
    if not manifest_path.is_file():
        return [f"missing_manifest:{cycle_id}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid_manifest:{cycle_id}:{exc}"]
    if not isinstance(manifest, dict):
        return [f"invalid_manifest_root:{cycle_id}"]

    errors: list[str] = []
    expected_fields = {
        "cycle_id": cycle_id,
        "actual_capture_date": str(row["actual_capture_date"]),
        "capture_phase": str(row["capture_phase"]),
        "acceptance": str(row["status"]),
    }
    for field, expected in expected_fields.items():
        if str(manifest.get(field, "")) != expected:
            errors.append(f"manifest_{field}_mismatch:{cycle_id}")

    outputs = manifest.get("outputs_sha256")
    if not isinstance(outputs, dict) or not outputs:
        errors.append(f"manifest_outputs_missing:{cycle_id}")
        return errors
    for raw_path, expected_sha in sorted(outputs.items()):
        artifact = (cycle_dir / str(raw_path)).resolve()
        try:
            artifact.relative_to(cycle_dir)
        except ValueError:
            errors.append(f"manifest_output_outside_cycle:{cycle_id}:{raw_path}")
            continue
        if not artifact.is_file():
            errors.append(f"manifest_output_missing:{cycle_id}:{raw_path}")
            continue
        if sha256_file(artifact) != str(expected_sha):
            errors.append(f"manifest_output_hash_mismatch:{cycle_id}:{raw_path}")
    return errors


def verified_summary_lines(
    *,
    rows: Mapping[str, Mapping[str, Any]],
    expected_phases: tuple[str, ...],
    required_phases: tuple[str, ...],
    zone: ZoneInfo,
    hash_errors: list[str],
    store_errors: list[str],
) -> tuple[list[str], bool]:
    """Build the human summary and the pass/fail verdict for one invocation.

    ``required_phases`` are the phases whose scheduled time has already passed
    today; each must have a successful capture row for the invocation to pass.
    Phases expected later today are listed as PENDING without failing.
    """
    if not expected_phases:
        return (
            ["**Independent provider ingestion: no capture phases scheduled today**"],
            not hash_errors and not store_errors,
        )
    missing_required = [phase for phase in required_phases if phase not in rows]
    verified = not missing_required and not hash_errors and not store_errors
    if hash_errors or store_errors:
        title = "**Independent provider ingestion verification failed:**"
    elif missing_required:
        title = "**Independent provider ingestion incomplete:**"
    elif not required_phases:
        title = "**Independent provider ingestion pending:**"
    elif set(required_phases) == {"premarket", "priority_refresh"}:
        title = "**Independent provider ingestion ran successfully this morning:**"
    else:
        title = "**Independent provider ingestion ran successfully:**"
    lines = [title]
    for phase in expected_phases:
        row = rows.get(phase)
        label = PHASE_LABELS[phase]
        if row is None:
            state = "MISSING" if phase in required_phases else "PENDING"
            lines.append(f"{label}: {state}")
            continue
        completed = _format_capture_time(str(row["completed_at_utc"]), zone)
        lines.append(f"{label}: {completed}, {row['status']}")

    completed_rows = [rows[phase] for phase in expected_phases if phase in rows]
    coverage = {
        (int(row["member_count"]), int(row["request_count"]))
        for row in completed_rows
    }
    if len(coverage) == 1:
        names, requests = next(iter(coverage))
        lines.append(f"{names} names, {requests} requests per cycle")
    elif completed_rows:
        detail = "; ".join(
            f"{PHASE_LABELS[phase]} {int(rows[phase]['member_count'])} names/"
            f"{int(rows[phase]['request_count'])} requests"
            for phase in expected_phases
            if phase in rows
        )
        lines.append(f"Cycle coverage differs: {detail}")

    request_errors = sum(int(row["error_request_count"]) for row in completed_rows)
    lines.append(
        "No request errors"
        if request_errors == 0
        else f"Request errors: {request_errors}"
    )
    lines.append("Output hashes: valid" if not hash_errors else "Output hashes: INVALID")
    lines.append(
        "Provider database validation: PASS"
        if not store_errors
        else "Provider database validation: FAIL"
    )
    for error in [*hash_errors, *store_errors][:10]:
        lines.append(f"Validation error: {error}")
    return lines, verified


def write_summary_log(
    *,
    output_root: Path,
    lines: list[str],
    verified: bool,
    invoked_at_utc: datetime,
) -> Path:
    """Persist the summary next to the capture artifacts (stdout is discarded
    by the Windows scheduled task, so the log is the durable record)."""
    output_root.mkdir(parents=True, exist_ok=True)
    entry = "\n".join(
        [
            f"[{invoked_at_utc.isoformat()}] verification="
            + ("PASS" if verified else "FAIL"),
            *lines,
        ]
    )
    log_path = output_root / SUMMARY_LOG_NAME
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n\n")
    latest_path = output_root / SUMMARY_LATEST_NAME
    staging_path = latest_path.with_name(SUMMARY_LATEST_NAME + ".tmp")
    staging_path.write_text(entry + "\n", encoding="utf-8")
    staging_path.replace(latest_path)
    return log_path


def _emit_verified_summary(
    *,
    config: dict[str, Any],
    config_path: Path,
    ingestion: Mapping[str, Any],
    now_local: datetime,
    now_utc: datetime,
) -> bool:
    local_date = now_local.date().isoformat()
    expected_phases, required_phases = scheduled_phase_progress(
        now_local=now_local,
        schedules=ingestion.get("schedules", {}),
    )
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    conn = connect_store(
        store_path,
        timeout_sec=float(ingestion.get("writer_lock_timeout_sec", 30.0)),
    )
    try:
        rows = _successful_phase_rows(
            conn,
            actual_capture_date=local_date,
            phases=expected_phases,
        )
        store_errors = verify_store(conn)
    finally:
        conn.close()

    runtime_paths = resolve_runtime_paths(config, config_path)
    output_root = ensure_not_prod_path(
        runtime_paths.output_dir
        / str(ingestion.get("output_subdir", "provider_ingestion")),
        label="provider ingestion output path",
    )
    hash_errors: list[str] = []
    for row in rows.values():
        hash_errors.extend(_capture_manifest_errors(row, output_root=output_root))
    summary_zone = (
        now_local.tzinfo
        if isinstance(now_local.tzinfo, ZoneInfo)
        else ZoneInfo("UTC")
    )
    lines, verified = verified_summary_lines(
        rows=rows,
        expected_phases=expected_phases,
        required_phases=required_phases,
        zone=summary_zone,
        hash_errors=hash_errors,
        store_errors=store_errors,
    )
    for line in lines:
        print(line)
    write_summary_log(
        output_root=output_root,
        lines=lines,
        verified=verified,
        invoked_at_utc=now_utc,
    )
    return verified


def run_selftest() -> None:
    schedules = {
        "sunday_baseline": "18:00",
        "premarket": "07:30",
        "priority_refresh": "08:45",
        "intraday": "disabled",
        "postclose": "18:00",
    }
    assert due_phase(
        now_local=datetime(2026, 8, 2, 18, 5), schedules=schedules, grace_minutes=20
    ) == "sunday_baseline"
    assert due_phase(
        now_local=datetime(2026, 8, 3, 7, 40), schedules=schedules, grace_minutes=20
    ) == "premarket"
    assert due_phase(
        now_local=datetime(2026, 8, 3, 10, 0), schedules=schedules, grace_minutes=20
    ) is None
    assert scheduled_phase_progress(
        now_local=datetime(2026, 8, 3, 10, 0), schedules=schedules
    ) == (("premarket", "priority_refresh", "postclose"), ("premarket", "priority_refresh"))
    assert scheduled_phase_progress(
        now_local=datetime(2026, 8, 3, 6, 0), schedules=schedules
    ) == (("premarket", "priority_refresh", "postclose"), ())
    assert scheduled_phase_progress(
        now_local=datetime(2026, 8, 1, 12, 0), schedules=schedules
    ) == ((), ())

    zone = ZoneInfo("America/New_York")
    morning_rows = {
        "premarket": {
            "completed_at_utc": "2026-08-03T11:36:40+00:00",
            "status": "PASS_WITH_WARNINGS",
            "member_count": 65,
            "request_count": 195,
            "error_request_count": 0,
        },
        "priority_refresh": {
            "completed_at_utc": "2026-08-03T12:56:40+00:00",
            "status": "PASS_WITH_WARNINGS",
            "member_count": 65,
            "request_count": 195,
            "error_request_count": 0,
        },
    }
    expected = ("premarket", "priority_refresh", "postclose")

    # Success, already-complete invocation later in the morning (both phases done).
    summary, verified = verified_summary_lines(
        rows=morning_rows,
        expected_phases=expected,
        required_phases=("premarket", "priority_refresh"),
        zone=zone,
        hash_errors=[],
        store_errors=[],
    )
    assert verified
    assert summary == [
        "**Independent provider ingestion ran successfully this morning:**",
        "Premarket: 7:37 AM ET, PASS_WITH_WARNINGS",
        "Priority refresh: 8:57 AM ET, PASS_WITH_WARNINGS",
        "Postclose: PENDING",
        "65 names, 195 requests per cycle",
        "No request errors",
        "Output hashes: valid",
        "Provider database validation: PASS",
    ]

    # Success, fresh premarket capture: later phases pending, still a pass.
    summary, verified = verified_summary_lines(
        rows={"premarket": morning_rows["premarket"]},
        expected_phases=expected,
        required_phases=("premarket",),
        zone=zone,
        hash_errors=[],
        store_errors=[],
    )
    assert verified
    assert summary[0] == "**Independent provider ingestion ran successfully:**"
    assert "Priority refresh: PENDING" in summary
    assert "Postclose: PENDING" in summary

    # Pending: invoked before any phase's scheduled time; honest, non-failing.
    summary, verified = verified_summary_lines(
        rows={},
        expected_phases=expected,
        required_phases=(),
        zone=zone,
        hash_errors=[],
        store_errors=[],
    )
    assert verified
    assert summary[0] == "**Independent provider ingestion pending:**"
    assert "Premarket: PENDING" in summary
    assert not any("successfully" in line for line in summary)

    # Incomplete: a scheduled phase elapsed without a successful capture.
    summary, verified = verified_summary_lines(
        rows={"premarket": morning_rows["premarket"]},
        expected_phases=expected,
        required_phases=("premarket", "priority_refresh"),
        zone=zone,
        hash_errors=[],
        store_errors=[],
    )
    assert not verified
    assert summary[0] == "**Independent provider ingestion incomplete:**"
    assert "Priority refresh: MISSING" in summary
    assert not any("successfully" in line for line in summary)

    # Validation failure: no success banner, hashes flagged invalid.
    summary, verified = verified_summary_lines(
        rows=morning_rows,
        expected_phases=expected,
        required_phases=("premarket", "priority_refresh"),
        zone=zone,
        hash_errors=["manifest_output_hash_mismatch:scheduled-x:capture_requests.csv"],
        store_errors=[],
    )
    assert not verified
    assert summary[0] == "**Independent provider ingestion verification failed:**"
    assert "Output hashes: INVALID" in summary
    assert (
        "Validation error: manifest_output_hash_mismatch:scheduled-x:capture_requests.csv"
        in summary
    )
    assert not any("successfully" in line for line in summary)

    # Store-validation failure is reported and fails the invocation.
    summary, verified = verified_summary_lines(
        rows=morning_rows,
        expected_phases=expected,
        required_phases=("premarket", "priority_refresh"),
        zone=zone,
        hash_errors=[],
        store_errors=["run_chain_break:run-1"],
    )
    assert not verified
    assert summary[0] == "**Independent provider ingestion verification failed:**"
    assert "Provider database validation: FAIL" in summary

    # Durable log: appended entries plus an atomic latest snapshot.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "provider_ingestion"
        invoked = datetime(2026, 8, 6, 13, 5, tzinfo=timezone.utc)
        log_path = write_summary_log(
            output_root=root,
            lines=["**Independent provider ingestion ran successfully this morning:**"],
            verified=True,
            invoked_at_utc=invoked,
        )
        write_summary_log(
            output_root=root,
            lines=["**Independent provider ingestion verification failed:**"],
            verified=False,
            invoked_at_utc=invoked + timedelta(minutes=10),
        )
        log_text = log_path.read_text(encoding="utf-8")
        assert log_text.count("[2026-08-06T13:") == 2
        assert "verification=PASS" in log_text and "verification=FAIL" in log_text
        latest = (root / SUMMARY_LATEST_NAME).read_text(encoding="utf-8")
        assert "verification=FAIL" in latest
        assert "verification=PASS" not in latest
    print("provider schedule dispatcher selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    if ingestion.get("missed_run_policy") != "current_only_no_backfill":
        raise ValueError("Scheduled provider capture must remain current-only")
    now_utc = args.now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("--now-utc must include a timezone")
    zone = ZoneInfo(str(ingestion.get("timezone", "America/New_York")))
    now_local = now_utc.astimezone(zone)
    schedules = ingestion.get("schedules", {})
    if not isinstance(schedules, dict):
        raise ValueError("provider_ingestion.schedules must be a mapping")
    phase = due_phase(
        now_local=now_local,
        schedules=schedules,
        grace_minutes=int(ingestion.get("schedule_grace_minutes", 20)),
    )
    if phase is None:
        print("PROVIDER SCHEDULE: PASS_NOOP; no current capture is due")
        verified = _emit_verified_summary(
            config=config,
            config_path=config_path,
            ingestion=ingestion,
            now_local=now_local,
            now_utc=now_utc,
        )
        return 0 if verified else 1
    local_date = now_local.date().isoformat()
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    conn = connect_store(
        store_path,
        timeout_sec=float(ingestion.get("writer_lock_timeout_sec", 30.0)),
    )
    try:
        attempts = conn.execute(
            "SELECT cycle_id,status FROM capture_runs "
            "WHERE actual_capture_date=? AND capture_phase=? AND cycle_id LIKE 'scheduled-%' "
            "ORDER BY rowid",
            (local_date, phase),
        ).fetchall()
    finally:
        conn.close()
    if any(str(row["status"]) in {"PASS", "PASS_WITH_WARNINGS"} for row in attempts):
        print(f"PROVIDER SCHEDULE: PASS_NOOP; {phase} already completed for {local_date}")
        verified = _emit_verified_summary(
            config=config,
            config_path=config_path,
            ingestion=ingestion,
            now_local=now_local,
            now_utc=now_utc,
        )
        return 0 if verified else 1
    max_attempts = int(ingestion.get("max_scheduled_attempts", 2))
    if len(attempts) >= max_attempts:
        raise RuntimeError(
            f"Provider schedule exhausted {max_attempts} attempts for {local_date}/{phase}"
        )
    cycle_id = (
        f"scheduled-{local_date.replace('-', '')}-{phase}-a{len(attempts) + 1:02d}"
    )
    command = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--config",
        str(config_path),
        "--phase",
        phase,
        "--portfolio-as-of",
        local_date,
        "--cycle-id",
        cycle_id,
    ]
    if args.dry_run:
        command.append("--dry-run")
        print("PROVIDER SCHEDULE: DRY_RUN; " + subprocess.list2cmdline(command))
        return 0
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        return int(completed.returncode)
    verified = _emit_verified_summary(
        config=config,
        config_path=config_path,
        ingestion=ingestion,
        now_local=now_local,
        now_utc=now_utc,
    )
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
