#!/usr/bin/env python3
"""Dispatch at most one current provider capture that is due under the ET schedule."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path  # noqa: E402
from portfolio_layer.provider_ingestion.store import connect_store  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CAPTURE_SCRIPT = Path(__file__).with_name("capture.py")


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


def due_phase(
    *,
    now_local: datetime,
    schedules: Mapping[str, Any],
    grace_minutes: int,
) -> str | None:
    weekday = now_local.weekday()
    allowed = (
        ("sunday_baseline",)
        if weekday == 6
        else ("premarket", "priority_refresh", "postclose")
        if weekday < 5
        else ()
    )
    current = now_local.hour * 60 + now_local.minute
    candidates: list[tuple[int, str]] = []
    for phase in allowed:
        configured = str(schedules.get(phase, "disabled")).strip().casefold()
        if configured == "disabled":
            continue
        lag = current - _minutes(configured)
        if 0 <= lag <= grace_minutes:
            candidates.append((lag, phase))
    return None if not candidates else min(candidates)[1]


def run_selftest() -> None:
    schedules = {
        "sunday_baseline": "18:00",
        "premarket": "07:30",
        "priority_refresh": "08:45",
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
        return 0
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
        return 0
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
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
