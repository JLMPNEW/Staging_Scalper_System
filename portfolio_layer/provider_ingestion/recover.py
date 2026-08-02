#!/usr/bin/env python3
"""Audit provider continuity and run missing portfolio sessions oldest-first."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.health import (  # noqa: E402
    CONTINUITY_FIELDS,
    capture_continuity_rows,
    continuity_gaps,
    expected_capture_slots,
    previous_or_same_session,
    session_dates,
    universe_freshness,
)
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    connect_store,
    digest,
    verify_store,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PORTFOLIO_RUNNER = PACKAGE_ROOT / "orchestration" / "18_run_portfolio_pipeline.py"
STEP_FIELDS = (
    "as_of_date",
    "status",
    "return_code",
    "orchestration_acceptance",
    "command",
    "detail",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--cadence", choices=("tactical", "strategic"), default="strategic")
    parser.add_argument("--groups", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run missing dates. Without this flag the command only seals a recovery plan.",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--now-utc", type=datetime.fromisoformat)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _accepted_run_dates(runs_root: Path) -> set[str]:
    accepted: set[str] = set()
    if not runs_root.is_dir():
        return accepted
    for run_dir in runs_root.iterdir():
        manifest_path = run_dir / "orchestration_meta.json"
        if not run_dir.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        acceptance = str(manifest.get("acceptance", ""))
        if (
            str(manifest.get("run_as_of", "")) == run_dir.name
            and acceptance.startswith("PASS")
        ):
            accepted.add(run_dir.name)
    return accepted


def _default_from_date(
    *, calendar_name: str, through: date, accepted: set[str], lookback_sessions: int
) -> date:
    calendar_start = through - timedelta(days=max(lookback_sessions * 3, 30))
    sessions = session_dates(calendar_name, calendar_start, through)
    recent = sessions[-max(lookback_sessions, 1) :]
    if not recent:
        raise ValueError(f"No {calendar_name} sessions end on or before {through}")
    missing = [value for value in recent if value.isoformat() not in accepted]
    return min(missing) if missing else recent[-1]


def _orchestration_acceptance(path: Path, expected_date: str) -> str:
    if not path.is_file():
        return "MISSING"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "INVALID"
    if str(payload.get("run_as_of", "")) != expected_date:
        return "DATE_MISMATCH"
    return str(payload.get("acceptance", "")) or "MISSING_ACCEPTANCE"


def _latest_universe_date(db_path: Path, timeout_sec: float) -> str:
    if not db_path.is_file():
        return ""
    conn = sqlite3.connect(str(db_path), timeout=timeout_sec)
    try:
        row = conn.execute("SELECT MAX(run_as_of) FROM monitor_universe").fetchone()
        return "" if row is None or row[0] is None else str(row[0])
    finally:
        conn.close()


def run_selftest() -> None:
    accepted = {"2026-07-30", "2026-07-31"}
    assert _default_from_date(
        calendar_name="XNYS",
        through=date(2026, 7, 31),
        accepted=accepted,
        lookback_sessions=2,
    ) == date(2026, 7, 31)
    assert _default_from_date(
        calendar_name="XNYS",
        through=date(2026, 7, 31),
        accepted={"2026-07-30"},
        lookback_sessions=2,
    ) == date(2026, 7, 31)
    print("provider delayed-run recovery selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    monitor = cfg_get(config, "expectations_monitor", {})
    if not isinstance(ingestion, dict) or not isinstance(monitor, dict):
        raise ValueError("provider_ingestion and expectations_monitor must be mappings")
    recovery = ingestion.get("recovery", {})
    if not isinstance(recovery, dict):
        raise ValueError("provider_ingestion.recovery must be a mapping")
    now_utc = args.now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("--now-utc must include a timezone")
    timezone_name = str(ingestion.get("timezone", "America/New_York"))
    calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
    local_today = now_utc.astimezone(ZoneInfo(timezone_name)).date()
    through = args.through or previous_or_same_session(calendar_name, local_today)
    if through > local_today:
        raise ValueError("--through cannot be in the future")

    accepted_dates = _accepted_run_dates(paths.output_dir / "runs")
    start = args.from_date or _default_from_date(
        calendar_name=calendar_name,
        through=through,
        accepted=accepted_dates,
        lookback_sessions=int(recovery.get("portfolio_catchup_lookback_sessions", 10)),
    )
    if start > through:
        raise ValueError("--from-date must be on or before --through")
    sessions = session_dates(calendar_name, start, through)
    missing_dates = [value for value in sessions if value.isoformat() not in accepted_dates]

    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
    conn = connect_store(store_path, timeout_sec=timeout)
    try:
        store_errors = verify_store(conn)
        slots = expected_capture_slots(
            start=start,
            end=through,
            now_utc=now_utc,
            schedules=dict(ingestion.get("schedules", {})),
            timezone_name=timezone_name,
            calendar_name=calendar_name,
            grace_minutes=int(ingestion.get("schedule_grace_minutes", 20)),
            service_started_on=date.fromisoformat(
                str(recovery.get("service_started_on", local_today.isoformat()))
            ),
        )
        continuity_rows = capture_continuity_rows(conn, slots=slots)
    finally:
        conn.close()
    gaps = continuity_gaps(continuity_rows)

    monitor_db = ensure_not_prod_path(
        resolve_path(
            monitor.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    universe_before = _latest_universe_date(monitor_db, timeout)
    steps: list[dict[str, Any]] = []
    blocked = bool(store_errors)
    if args.execute and not blocked:
        for run_date in missing_dates:
            value = run_date.isoformat()
            command = [
                sys.executable,
                str(PORTFOLIO_RUNNER),
                "--config",
                str(config_path),
                "--as-of",
                value,
                "--cadence",
                args.cadence,
            ]
            if args.groups.strip():
                command.extend(["--groups", args.groups.strip()])
            if run_date < local_today:
                command.append("--historical-catchup")
            if args.force:
                command.append("--force")
            completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
            acceptance = _orchestration_acceptance(
                paths.output_dir / "runs" / value / "orchestration_meta.json",
                value,
            )
            passed = completed.returncode == 0 and acceptance.startswith("PASS")
            steps.append(
                {
                    "as_of_date": value,
                    "status": "PASS" if passed else "FAIL",
                    "return_code": completed.returncode,
                    "orchestration_acceptance": acceptance,
                    "command": subprocess.list2cmdline(command),
                    "detail": "processed oldest-first" if passed else "catch-up stopped fail-closed",
                }
            )
            if not passed:
                blocked = True
                break
    else:
        for run_date in missing_dates:
            value = run_date.isoformat()
            steps.append(
                {
                    "as_of_date": value,
                    "status": "PLANNED" if not store_errors else "BLOCKED_STORE_INVALID",
                    "return_code": "",
                    "orchestration_acceptance": "",
                    "command": "",
                    "detail": "execute flag required; no provider endpoint will be replayed",
                }
            )

    universe_after = _latest_universe_date(monitor_db, timeout)
    universe_readiness: dict[str, Any]
    if universe_after:
        universe_readiness = universe_freshness(
            calendar_name,
            actual_date=local_today,
            phase="priority_refresh",
            universe_as_of=universe_after,
        )
    else:
        universe_readiness = {
            "status": "MISSING",
            "universe_as_of": "",
            "expected_universe_as_of": "",
            "lag_sessions": "",
        }

    universe_warning = universe_readiness["status"] != "CURRENT"
    if store_errors or blocked:
        acceptance = "FAIL"
    elif not args.execute:
        acceptance = "PLAN_WITH_WARNINGS" if gaps or universe_warning else "PLAN"
    elif gaps or universe_warning:
        acceptance = "PASS_WITH_WARNINGS"
    else:
        acceptance = "PASS"
    cycle = now_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir
        / str(ingestion.get("output_subdir", "provider_ingestion"))
        / "recovery"
        / cycle
    )
    continuity_path = output_dir / "provider_capture_continuity.csv"
    steps_path = output_dir / "portfolio_catchup_steps.csv"
    manifest_path = output_dir / "provider_recovery_manifest.json"
    write_csv(continuity_path, CONTINUITY_FIELDS, continuity_rows)
    write_csv(steps_path, STEP_FIELDS, steps)
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_delayed_run_recovery_v1",
            "acceptance": acceptance,
            "executed": bool(args.execute),
            "from_date": start.isoformat(),
            "through_date": through.isoformat(),
            "sessions_considered": [value.isoformat() for value in sessions],
            "missing_portfolio_dates": [value.isoformat() for value in missing_dates],
            "provider_store_errors": store_errors,
            "capture_gap_count": len(gaps),
            "capture_gaps": gaps,
            "missed_capture_policy": "flag_no_backfill",
            "historical_current_snapshot_calls_made": False,
            "historical_provider_event_cycles_suppressed": True,
            "universe_as_of_before": universe_before,
            "universe_as_of_after": universe_after,
            "priority_capture_universe_readiness": universe_readiness,
            "step_digest": digest(steps),
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("health.py")): sha256_file(
                    Path(__file__).with_name("health.py")
                ),
                str(Path(__file__).with_name("store.py")): sha256_file(
                    Path(__file__).with_name("store.py")
                ),
            },
            "outputs_sha256": {
                continuity_path.name: sha256_file(continuity_path),
                steps_path.name: sha256_file(steps_path),
            },
        },
    )
    print(
        f"PROVIDER RECOVERY: {acceptance}; missing_dates={len(missing_dates)}; "
        f"capture_gaps={len(gaps)}; output={manifest_path}"
    )
    return 1 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
