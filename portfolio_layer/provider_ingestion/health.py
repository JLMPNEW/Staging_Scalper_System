"""Schedule-continuity and universe-freshness checks for provider ingestion."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


CONTINUITY_FIELDS = (
    "capture_date",
    "capture_phase",
    "due_at_utc",
    "status",
    "attempt_count",
    "accepted_attempt_count",
    "failed_attempt_count",
    "latest_cycle_id",
    "latest_completed_at_utc",
    "detail",
)
ACCEPTED_CAPTURE_STATUSES = frozenset({"PASS", "PASS_WITH_WARNINGS"})


def _calendar(calendar_name: str) -> Any:
    import exchange_calendars as xcals  # type: ignore[import-untyped]

    return xcals.get_calendar(calendar_name)


def _timestamp(value: date) -> Any:
    import pandas as pd  # type: ignore[import-untyped]

    return pd.Timestamp(value)


def session_dates(calendar_name: str, start: date, end: date) -> list[date]:
    """Return exchange sessions in an inclusive calendar-date interval."""
    if end < start:
        return []
    sessions = _calendar(calendar_name).sessions_in_range(_timestamp(start), _timestamp(end))
    return [value.date() for value in sessions]


def previous_or_same_session(calendar_name: str, value: date) -> date:
    calendar = _calendar(calendar_name)
    return calendar.date_to_session(_timestamp(value), direction="previous").date()


def prior_session(calendar_name: str, value: date) -> date:
    calendar = _calendar(calendar_name)
    session = calendar.date_to_session(_timestamp(value), direction="previous")
    if session.date() == value:
        session = calendar.previous_session(session)
    return session.date()


def expected_universe_session(
    calendar_name: str,
    *,
    actual_date: date,
    phase: str,
) -> date:
    """Latest portfolio universe reasonably available when a phase begins."""
    calendar = _calendar(calendar_name)
    session = calendar.date_to_session(_timestamp(actual_date), direction="previous")
    is_session = session.date() == actual_date
    if phase == "postclose" and is_session:
        return actual_date
    if is_session:
        session = calendar.previous_session(session)
    return session.date()


def universe_freshness(
    calendar_name: str,
    *,
    actual_date: date,
    phase: str,
    universe_as_of: str,
) -> dict[str, Any]:
    expected = expected_universe_session(
        calendar_name,
        actual_date=actual_date,
        phase=phase,
    )
    source = date.fromisoformat(universe_as_of)
    if source >= expected:
        lag = 0
    else:
        lag = len(session_dates(calendar_name, source + timedelta(days=1), expected))
    return {
        "status": "CURRENT" if lag == 0 else "STALE",
        "universe_as_of": source.isoformat(),
        "expected_universe_as_of": expected.isoformat(),
        "lag_sessions": lag,
    }


def _configured_minutes(value: Any) -> tuple[int, int] | None:
    configured = str(value).strip().casefold()
    if not configured or configured == "disabled":
        return None
    hour_text, minute_text = configured.split(":", maxsplit=1)
    parsed = time(int(hour_text), int(minute_text))
    return parsed.hour, parsed.minute


def expected_capture_slots(
    *,
    start: date,
    end: date,
    now_utc: datetime,
    schedules: Mapping[str, Any],
    timezone_name: str,
    calendar_name: str,
    grace_minutes: int,
    service_started_on: date,
) -> list[dict[str, str]]:
    """Enumerate elapsed scheduled slots; future/in-flight slots are excluded."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must include a timezone")
    effective_start = max(start, service_started_on)
    if end < effective_start:
        return []
    zone = ZoneInfo(timezone_name)
    sessions = set(session_dates(calendar_name, effective_start, end))
    output: list[dict[str, str]] = []
    cursor = effective_start
    while cursor <= end:
        phases: Sequence[str]
        if cursor.weekday() == 6:
            phases = ("sunday_baseline",)
        elif cursor in sessions:
            phases = ("premarket", "priority_refresh", "postclose")
        else:
            phases = ()
        for phase in phases:
            parsed = _configured_minutes(schedules.get(phase, "disabled"))
            if parsed is None:
                continue
            due_local = datetime.combine(cursor, time(*parsed), tzinfo=zone)
            due_utc = due_local.astimezone(timezone.utc)
            if due_utc + timedelta(minutes=max(grace_minutes, 0)) > now_utc.astimezone(
                timezone.utc
            ):
                continue
            output.append(
                {
                    "capture_date": cursor.isoformat(),
                    "capture_phase": phase,
                    "due_at_utc": due_utc.replace(microsecond=0).isoformat(),
                }
            )
        cursor += timedelta(days=1)
    return output


def capture_continuity_rows(
    conn: sqlite3.Connection,
    *,
    slots: Sequence[Mapping[str, str]],
    table_prefix: str = "",
) -> list[dict[str, Any]]:
    """Compare elapsed schedule slots with append-only capture-run evidence."""
    if table_prefix and not table_prefix.endswith("."):
        raise ValueError("table_prefix must be empty or end with '.'")
    output: list[dict[str, Any]] = []
    for slot in slots:
        attempts = conn.execute(
            f"SELECT cycle_id,status,completed_at_utc FROM {table_prefix}capture_runs "
            "WHERE actual_capture_date=? AND capture_phase=? "
            "ORDER BY completed_at_utc,cycle_id",
            (slot["capture_date"], slot["capture_phase"]),
        ).fetchall()
        accepted = [row for row in attempts if str(row["status"]) in ACCEPTED_CAPTURE_STATUSES]
        failed = [row for row in attempts if str(row["status"]) == "FAIL"]
        latest = attempts[-1] if attempts else None
        status = "PASS" if accepted else "FAILED" if attempts else "MISSING"
        output.append(
            {
                **dict(slot),
                "status": status,
                "attempt_count": len(attempts),
                "accepted_attempt_count": len(accepted),
                "failed_attempt_count": len(failed),
                "latest_cycle_id": "" if latest is None else str(latest["cycle_id"]),
                "latest_completed_at_utc": (
                    "" if latest is None else str(latest["completed_at_utc"])
                ),
                "detail": (
                    "accepted scheduled capture exists"
                    if accepted
                    else "scheduled attempts did not produce an accepted capture"
                    if attempts
                    else "elapsed scheduled slot has no capture attempt; no backfill permitted"
                ),
            }
        )
    return output


def continuity_gaps(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("status")) != "PASS"]
