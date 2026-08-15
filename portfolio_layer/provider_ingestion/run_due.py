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
import csv
import json
import os
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
from portfolio_layer.core.contracts import (  # noqa: E402
    sha256_file,
    write_manifest,
    write_text_atomic,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.core.runtime_env import (  # noqa: E402
    hydrate_missing_user_environment,
)
from portfolio_layer.provider_ingestion.artifacts import (  # noqa: E402
    ensure_capture_manifest,
)
from portfolio_layer.provider_ingestion.health import (  # noqa: E402
    phase_grace_minutes,
    scheduled_capture_phases,
    validate_provider_ingestion_policy,
)
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    connect_store,
    connect_store_readonly,
    finalize_dispatch_attempt,
    record_dispatch_started,
    interrupt_stale_dispatch_attempts,
    verify_store,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CAPTURE_SCRIPT = Path(__file__).with_name("capture.py")
SUMMARY_LOG_NAME = "run_due_summary.log"
SUMMARY_LATEST_NAME = "run_due_summary_latest.txt"
SUMMARY_LOG_MAX_BYTES = 2_000_000
SUMMARY_LOG_BACKUP_COUNT = 3
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


def due_phases(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
    grace_minutes: int | Mapping[str, Any],
    calendar_name: str = "XNYS",
) -> tuple[str, ...]:
    current = now_local.hour * 60 + now_local.minute
    candidates: list[tuple[int, str]] = []
    for phase in scheduled_capture_phases(now_local.date(), calendar_name=calendar_name):
        configured = str(schedules.get(phase, "disabled")).strip().casefold()
        if configured == "disabled":
            continue
        scheduled_minute = _minutes(configured)
        lag = current - scheduled_minute
        if 0 <= lag <= phase_grace_minutes(phase, grace_minutes):
            candidates.append((scheduled_minute, phase))
    return tuple(phase for _, phase in sorted(candidates))


def due_phase(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
    grace_minutes: int | Mapping[str, Any],
    calendar_name: str = "XNYS",
) -> str | None:
    phases = due_phases(
        now_local=now_local,
        schedules=schedules,
        grace_minutes=grace_minutes,
        calendar_name=calendar_name,
    )
    return phases[0] if phases else None


def scheduled_phase_progress(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
    calendar_name: str = "XNYS",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return today's exchange-calendar phases and those whose time has passed."""
    current = now_local.hour * 60 + now_local.minute
    expected: list[str] = []
    required: list[str] = []
    for phase in scheduled_capture_phases(now_local.date(), calendar_name=calendar_name):
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
    local = (completed.astimezone(zone) + timedelta(seconds=30)).replace(second=0, microsecond=0)
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
    cycle_root = output_root.resolve()
    cycle_id = str(row["cycle_id"])
    cycle_dir = (cycle_root / cycle_id).resolve()
    try:
        cycle_dir.relative_to(cycle_root)
    except ValueError:
        return [f"cycle_path_outside_output_root:{cycle_id}"]
    if Path(cycle_id).name != cycle_id:
        return [f"invalid_cycle_id_path:{cycle_id}"]
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

    if manifest.get("raw_payloads_retained") is not False:
        errors.append(f"manifest_raw_payload_policy_mismatch:{cycle_id}")
    store_result = manifest.get("store_result")
    if not isinstance(store_result, dict):
        errors.append(f"manifest_store_result_missing:{cycle_id}")
    else:
        expected_store = {
            "run_id": str(row["run_id"]),
            "run_digest": str(row["run_digest"]),
            "status": str(row["status"]),
            "request_count": int(row["request_count"]),
            "normalized_row_count": int(row["normalized_row_count"]),
            "new_version_count": int(row["new_version_count"]),
            "unchanged_observation_count": int(row["unchanged_observation_count"]),
            "previous_pass_digest": str(row["previous_pass_digest"]),
        }
        for field, expected in expected_store.items():
            actual = store_result.get(field)
            if isinstance(expected, int):
                if actual is None:
                    matches = False
                else:
                    try:
                        matches = int(actual) == expected
                    except (TypeError, ValueError):
                        matches = False
            else:
                matches = str(actual) == expected
            if not matches:
                errors.append(f"manifest_store_{field}_mismatch:{cycle_id}")
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
        actual_sha = sha256_file(artifact)
        if actual_sha != str(expected_sha):
            errors.append(f"manifest_output_hash_mismatch:{cycle_id}:{raw_path}")
            continue
        if artifact.name == "capture_requests.csv":
            try:
                with artifact.open("r", encoding="utf-8-sig", newline="") as handle:
                    report_rows = list(csv.DictReader(handle))
            except (OSError, csv.Error) as exc:
                errors.append(f"manifest_report_invalid:{cycle_id}:{type(exc).__name__}")
                continue
            if len(report_rows) != int(row["request_count"]):
                errors.append(f"manifest_report_request_count_mismatch:{cycle_id}")
            available = sum(item.get("status") == "AVAILABLE" for item in report_rows)
            empty = sum(item.get("status") == "EMPTY" for item in report_rows)
            report_errors = len(report_rows) - available - empty
            if available != int(row["available_request_count"]):
                errors.append(f"manifest_report_available_count_mismatch:{cycle_id}")
            if empty != int(row["empty_request_count"]):
                errors.append(f"manifest_report_empty_count_mismatch:{cycle_id}")
            if report_errors != int(row["error_request_count"]):
                errors.append(f"manifest_report_error_count_mismatch:{cycle_id}")
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
    coverage = {(int(row["member_count"]), int(row["request_count"])) for row in completed_rows}
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
    lines.append("No request errors" if request_errors == 0 else f"Request errors: {request_errors}")
    lines.append("Output hashes: valid" if not hash_errors else "Output hashes: INVALID")
    lines.append("Provider database validation: PASS" if not store_errors else "Provider database validation: FAIL")
    for error in [*hash_errors, *store_errors][:10]:
        lines.append(f"Validation error: {error}")
    return lines, verified


def _rotate_summary_log(log_path: Path, *, incoming_bytes: int) -> None:
    if not log_path.is_file() or log_path.stat().st_size + incoming_bytes <= SUMMARY_LOG_MAX_BYTES:
        return
    log_path.with_name(f"{SUMMARY_LOG_NAME}.{SUMMARY_LOG_BACKUP_COUNT}").unlink(missing_ok=True)
    for index in range(SUMMARY_LOG_BACKUP_COUNT - 1, 0, -1):
        source = log_path.with_name(f"{SUMMARY_LOG_NAME}.{index}")
        if source.exists():
            source.replace(log_path.with_name(f"{SUMMARY_LOG_NAME}.{index + 1}"))
    log_path.replace(log_path.with_name(f"{SUMMARY_LOG_NAME}.1"))


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
            f"[{invoked_at_utc.isoformat()}] verification=" + ("PASS" if verified else "FAIL"),
            *lines,
        ]
    )
    encoded_entry = (entry + "\n\n").encode("utf-8")
    log_path = output_root / SUMMARY_LOG_NAME
    summary_lock = output_root / ".run_due_summary.lock"
    with writer_lock(summary_lock, timeout_sec=10.0):
        _rotate_summary_log(log_path, incoming_bytes=len(encoded_entry))
        with log_path.open("ab") as handle:
            handle.write(encoded_entry)
            handle.flush()
            os.fsync(handle.fileno())
        latest_path = output_root / SUMMARY_LATEST_NAME
        staging_path = latest_path.with_name(SUMMARY_LATEST_NAME + ".tmp")
        staging_path.write_text(entry + "\n", encoding="utf-8")
        staging_path.replace(latest_path)
    return log_path


def _provider_output_root(
    config: Mapping[str, Any],
    config_path: Path,
    ingestion: Mapping[str, Any],
) -> Path:
    runtime_paths = resolve_runtime_paths(dict(config), config_path)
    return ensure_not_prod_path(
        runtime_paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")),
        label="provider ingestion output path",
    )


def _scheduled_attempt_ids(
    *,
    output_root: Path,
    local_date: str,
    phase: str,
    database_cycle_ids: set[str],
) -> set[str]:
    prefix = f"scheduled-{local_date.replace('-', '')}-{phase}-a"
    disk_ids = {
        child.name for child in output_root.glob(f"{prefix}*") if child.is_dir() and child.name[len(prefix) :].isdigit()
    }
    return database_cycle_ids | disk_ids


def _next_attempt_number(attempt_ids: set[str]) -> int:
    suffixes: list[int] = []
    for cycle_id in attempt_ids:
        marker = cycle_id.rsplit("-a", maxsplit=1)
        if len(marker) == 2 and marker[1].isdigit():
            suffixes.append(int(marker[1]))
    return max(suffixes, default=0) + 1


def _redact_child_output(value: str) -> str:
    redacted = value
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    for name, secret in os.environ.items():
        if secret and len(secret) >= 8 and any(marker in name.upper() for marker in secret_markers):
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _write_scheduler_attempt(
    *,
    output_root: Path,
    cycle_id: str,
    command: list[str],
    invoked_at_utc: datetime,
    completed: subprocess.CompletedProcess[str] | None = None,
    completed_at_utc: datetime | None = None,
) -> Path:
    cycle_root = output_root.resolve()
    cycle_dir = (cycle_root / cycle_id).resolve()
    try:
        cycle_dir.relative_to(cycle_root)
    except ValueError as exc:
        raise ValueError(f"Scheduler cycle path escapes output root: {cycle_id}") from exc
    if Path(cycle_id).name != cycle_id:
        raise ValueError(f"Scheduler cycle ID must be a single path component: {cycle_id}")
    cycle_dir.mkdir(parents=True, exist_ok=True)
    stdout = _redact_child_output("" if completed is None else completed.stdout or "")
    stderr = _redact_child_output("" if completed is None else completed.stderr or "")
    log_text = "".join(("[stdout]\n", stdout, "\n[stderr]\n", stderr, "\n"))
    log_path = cycle_dir / "scheduler_child.log"
    write_text_atomic(log_path, log_text)
    returncode = None if completed is None else int(completed.returncode)
    state = "STARTED" if completed is None else "PASS" if returncode == 0 else "FAIL"
    manifest_path = cycle_dir / "scheduler_attempt.json"
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_scheduler_attempt_v2",
            "cycle_id": cycle_id,
            "started_at_utc": invoked_at_utc.isoformat(),
            "completed_at_utc": ("" if completed_at_utc is None else completed_at_utc.isoformat()),
            "command": command,
            "returncode": returncode,
            "acceptance": state,
            "child_log": log_path.name,
            "child_log_sha256": sha256_file(log_path),
            "inputs_sha256": {
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(CAPTURE_SCRIPT.resolve()): sha256_file(CAPTURE_SCRIPT.resolve()),
            },
        },
    )
    return manifest_path


def _emit_verified_summary(
    *,
    config: dict[str, Any],
    config_path: Path,
    ingestion: Mapping[str, Any],
    now_local: datetime,
    now_utc: datetime,
    persist_operational_artifacts: bool,
) -> bool:
    local_date = now_local.date().isoformat()
    calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
    expected_phases, required_phases = scheduled_phase_progress(
        now_local=now_local,
        schedules=ingestion.get("schedules", {}),
        calendar_name=calendar_name,
    )
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    output_root = _provider_output_root(config, config_path, ingestion)
    timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
    conn = connect_store_readonly(store_path, timeout_sec=timeout)
    recovery_errors: list[str] = []
    conn.execute("BEGIN")
    try:
        rows = _successful_phase_rows(
            conn,
            actual_capture_date=local_date,
            phases=expected_phases,
        )
        if persist_operational_artifacts:
            for row in rows.values():
                _, errors = ensure_capture_manifest(
                    conn,
                    row=row,
                    output_root=output_root,
                    store_path=store_path,
                )
                recovery_errors.extend(errors)
        store_errors = verify_store(conn)
    finally:
        conn.rollback()
        conn.close()

    hash_errors = list(recovery_errors)
    for row in rows.values():
        hash_errors.extend(_capture_manifest_errors(row, output_root=output_root))
    summary_zone = now_local.tzinfo if isinstance(now_local.tzinfo, ZoneInfo) else ZoneInfo("UTC")
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
    if persist_operational_artifacts:
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
    assert due_phase(now_local=datetime(2026, 8, 2, 18, 5), schedules=schedules, grace_minutes=20) == "sunday_baseline"
    assert due_phase(now_local=datetime(2026, 8, 3, 7, 40), schedules=schedules, grace_minutes=20) == "premarket"
    assert due_phase(now_local=datetime(2026, 8, 3, 10, 0), schedules=schedules, grace_minutes=20) is None
    assert scheduled_phase_progress(now_local=datetime(2026, 8, 3, 10, 0), schedules=schedules) == (
        ("premarket", "priority_refresh", "postclose"),
        ("premarket", "priority_refresh"),
    )
    assert scheduled_phase_progress(now_local=datetime(2026, 8, 3, 6, 0), schedules=schedules) == (
        ("premarket", "priority_refresh", "postclose"),
        (),
    )
    assert scheduled_phase_progress(now_local=datetime(2026, 8, 1, 12, 0), schedules=schedules) == ((), ())

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
    assert "Validation error: manifest_output_hash_mismatch:scheduled-x:capture_requests.csv" in summary
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
    hydrate_missing_user_environment()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    validate_provider_ingestion_policy(ingestion)
    now_utc = args.now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("--now-utc must include a timezone")
    if args.now_utc is not None and not args.dry_run:
        raise ValueError("--now-utc is a simulated clock and requires --dry-run")
    zone = ZoneInfo(str(ingestion.get("timezone", "America/New_York")))
    calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
    now_local = now_utc.astimezone(zone)
    schedules = ingestion.get("schedules", {})
    if not isinstance(schedules, dict):
        raise ValueError("provider_ingestion.schedules must be a mapping")
    grace_config = ingestion.get("phase_grace_minutes", int(ingestion.get("schedule_grace_minutes", 20)))
    timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    writer_path = store_path.with_suffix(store_path.suffix + ".writer.lock")
    capture_timeout_minutes = float(ingestion.get("capture_timeout_minutes", 100.0))
    stale_minutes = float(ingestion.get("dispatch_stale_minutes", 110.0))
    if capture_timeout_minutes <= 0:
        raise ValueError("capture_timeout_minutes must be positive")
    if stale_minutes <= capture_timeout_minutes:
        raise ValueError("dispatch_stale_minutes must exceed capture_timeout_minutes")
    if not args.dry_run:
        stale_before = now_utc.astimezone(timezone.utc) - timedelta(minutes=stale_minutes)
        with writer_lock(writer_path, timeout_sec=timeout):
            conn = connect_store(store_path, timeout_sec=timeout)
            try:
                interrupted = interrupt_stale_dispatch_attempts(
                    conn,
                    stale_before_utc=stale_before.isoformat(),
                    interrupted_at_utc=now_utc.astimezone(timezone.utc).isoformat(),
                )
            finally:
                conn.close()
        if interrupted:
            print("PROVIDER SCHEDULE: recovered interrupted dispatches: " + ",".join(interrupted))
    phases = due_phases(
        now_local=now_local,
        schedules=schedules,
        grace_minutes=grace_config,
        calendar_name=calendar_name,
    )
    if not phases:
        print("PROVIDER SCHEDULE: PASS_NOOP; no current capture is due")
        verified = _emit_verified_summary(
            config=config,
            config_path=config_path,
            ingestion=ingestion,
            now_local=now_local,
            now_utc=now_utc,
            persist_operational_artifacts=not args.dry_run,
        )
        return 0 if verified else 1

    local_date = now_local.date().isoformat()
    output_root = _provider_output_root(config, config_path, ingestion)
    conn = connect_store_readonly(store_path, timeout_sec=timeout)
    try:
        capture_attempts = [
            dict(row)
            for row in conn.execute(
                "SELECT cycle_id,capture_phase,status FROM capture_runs "
                "WHERE actual_capture_date=? AND cycle_id LIKE 'scheduled-%' "
                "ORDER BY rowid",
                (local_date,),
            ).fetchall()
        ]
        dispatch_attempts = [
            dict(row)
            for row in conn.execute(
                "SELECT cycle_id,capture_phase,state FROM scheduled_dispatch_attempts "
                "WHERE actual_capture_date=? ORDER BY started_at_utc,cycle_id",
                (local_date,),
            ).fetchall()
        ]
    finally:
        conn.close()

    max_attempts = int(ingestion.get("max_scheduled_attempts", 2))
    if max_attempts <= 0:
        raise ValueError("max_scheduled_attempts must be positive")
    selected_phase: str | None = None
    selected_attempt_ids: set[str] = set()
    exhausted: list[str] = []
    for candidate in phases:
        candidate_captures = [row for row in capture_attempts if str(row["capture_phase"]) == candidate]
        if any(str(row["status"]) in {"PASS", "PASS_WITH_WARNINGS"} for row in candidate_captures):
            continue
        database_ids = {
            str(row["cycle_id"])
            for row in [*candidate_captures, *dispatch_attempts]
            if str(row["capture_phase"]) == candidate
        }
        attempt_ids = _scheduled_attempt_ids(
            output_root=output_root,
            local_date=local_date,
            phase=candidate,
            database_cycle_ids=database_ids,
        )
        if len(attempt_ids) >= max_attempts:
            exhausted.append(candidate)
            continue
        selected_phase = candidate
        selected_attempt_ids = attempt_ids
        break

    if selected_phase is None:
        if exhausted:
            print(
                "PROVIDER SCHEDULE: FAIL; exhausted attempts for "
                + ", ".join(f"{local_date}/{phase}" for phase in exhausted)
            )
        else:
            print(f"PROVIDER SCHEDULE: PASS_NOOP; all due phases already completed for {local_date}")
        verified = _emit_verified_summary(
            config=config,
            config_path=config_path,
            ingestion=ingestion,
            now_local=now_local,
            now_utc=now_utc,
            persist_operational_artifacts=not args.dry_run,
        )
        return 0 if verified and not exhausted else 1

    phase = selected_phase
    attempt_number = _next_attempt_number(selected_attempt_ids)
    cycle_id = f"scheduled-{local_date.replace('-', '')}-{phase}-a{attempt_number:02d}"
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

    with writer_lock(writer_path, timeout_sec=timeout):
        conn = connect_store(store_path, timeout_sec=timeout)
        try:
            record_dispatch_started(
                conn,
                cycle_id=cycle_id,
                actual_capture_date=local_date,
                capture_phase=phase,
                started_at_utc=now_utc.astimezone(timezone.utc).isoformat(),
                parent_pid=os.getpid(),
            )
        finally:
            conn.close()
    try:
        _write_scheduler_attempt(
            output_root=output_root,
            cycle_id=cycle_id,
            command=command,
            invoked_at_utc=now_utc,
        )
    except Exception:
        terminal = datetime.now(timezone.utc)
        with writer_lock(writer_path, timeout_sec=timeout):
            conn = connect_store(store_path, timeout_sec=timeout)
            try:
                finalize_dispatch_attempt(
                    conn,
                    cycle_id=cycle_id,
                    completed_at_utc=terminal.isoformat(),
                    state="FAIL",
                    return_code=126,
                    artifact_path="",
                    artifact_sha256="",
                    detail="scheduler_start_artifact_failure",
                )
            finally:
                conn.close()
        raise

    capture_timeout = capture_timeout_minutes * 60.0
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=capture_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        completed = subprocess.CompletedProcess(
            command,
            124,
            stdout,
            stderr + "\nprovider capture exceeded configured timeout\n",
        )
    except Exception as exc:
        completed = subprocess.CompletedProcess(
            command,
            127,
            "",
            f"provider child launch exception:{type(exc).__name__}\n",
        )

    terminal = datetime.now(timezone.utc)
    attempt_manifest: Path | None = None
    attempt_artifact_error = ""
    try:
        attempt_manifest = _write_scheduler_attempt(
            output_root=output_root,
            cycle_id=cycle_id,
            command=command,
            completed=completed,
            invoked_at_utc=now_utc,
            completed_at_utc=terminal,
        )
    except Exception as exc:
        attempt_artifact_error = type(exc).__name__
    terminal_return_code = int(completed.returncode) if not attempt_artifact_error else 125
    with writer_lock(writer_path, timeout_sec=timeout):
        conn = connect_store(store_path, timeout_sec=timeout)
        try:
            finalize_dispatch_attempt(
                conn,
                cycle_id=cycle_id,
                completed_at_utc=terminal.isoformat(),
                state="PASS" if terminal_return_code == 0 else "FAIL",
                return_code=terminal_return_code,
                artifact_path=("" if attempt_manifest is None else str(attempt_manifest.resolve())),
                artifact_sha256=("" if attempt_manifest is None else sha256_file(attempt_manifest)),
                detail=(
                    f"scheduler_terminal_artifact_failure:{attempt_artifact_error}"
                    if attempt_artifact_error
                    else f"child_return_code={int(completed.returncode)}"
                ),
            )
        finally:
            conn.close()
    if attempt_artifact_error:
        print(
            f"PROVIDER SCHEDULE: terminal attempt artifact could not be sealed: {attempt_artifact_error}",
            file=sys.stderr,
        )

    if completed.stdout:
        print(_redact_child_output(completed.stdout), end="")
    if completed.stderr:
        print(_redact_child_output(completed.stderr), end="", file=sys.stderr)
    verified = _emit_verified_summary(
        config=config,
        config_path=config_path,
        ingestion=ingestion,
        now_local=now_local,
        now_utc=now_utc,
        persist_operational_artifacts=not args.dry_run,
    )
    return 0 if terminal_return_code == 0 and verified else int(terminal_return_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
