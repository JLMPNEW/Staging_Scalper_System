"""Schedule-continuity and universe-freshness checks for provider ingestion."""

from __future__ import annotations

import math
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


EXPECTED_POLICY_VERSION = "provider_observation_store_v2"
EXPECTED_RECOVERY_POLICY_VERSION = "provider_delayed_run_recovery_v1"
EXPECTED_PROVIDERS = frozenset({"alpha_vantage", "fmp"})
SCHEDULED_PHASES = ("sunday_baseline", "premarket", "priority_refresh", "postclose")


def _positive_number(value: Any, *, label: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _fraction(value: Any, *, label: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{label} must be in [0, 1]")
    return parsed


def validate_provider_ingestion_policy(ingestion: Mapping[str, Any]) -> None:
    """Fail closed when declared provider-ingestion policy drifts from runtime behavior."""
    if ingestion.get("policy_version") != EXPECTED_POLICY_VERSION:
        raise ValueError(f"provider_ingestion.policy_version must be {EXPECTED_POLICY_VERSION}")
    if ingestion.get("enabled") is not True:
        raise ValueError("provider_ingestion.enabled must be true while the scheduled service is installed")
    if ingestion.get("network_owner") != "independent_service":
        raise ValueError("provider_ingestion.network_owner must be independent_service")
    if ingestion.get("raw_payload_retention_enabled") is not False:
        raise ValueError("Raw provider payload retention must be false")
    if ingestion.get("missed_run_policy") != "current_only_no_backfill":
        raise ValueError("Scheduled provider capture must remain current-only")

    capabilities = ingestion.get("managed_capabilities")
    if not isinstance(capabilities, Sequence) or isinstance(capabilities, (str, bytes)):
        raise ValueError("provider_ingestion.managed_capabilities must be a sequence")
    if {str(value) for value in capabilities} != {"estimates"}:
        raise ValueError("Independent ingestion may manage only the estimates capability")
    providers = ingestion.get("providers")
    if not isinstance(providers, Sequence) or isinstance(providers, (str, bytes)):
        raise ValueError("provider_ingestion.providers must be a sequence")
    provider_set = {str(value) for value in providers}
    if provider_set != EXPECTED_PROVIDERS or len(providers) != len(EXPECTED_PROVIDERS):
        raise ValueError(f"provider_ingestion.providers must contain exactly {sorted(EXPECTED_PROVIDERS)}")

    timezone_name = str(ingestion.get("timezone", ""))
    if timezone_name != "America/New_York":
        raise ValueError("provider_ingestion.timezone must be America/New_York")
    ZoneInfo(timezone_name)
    calendar_name = str(ingestion.get("exchange_calendar", ""))
    if calendar_name != "XNYS":
        raise ValueError("provider_ingestion.exchange_calendar must be XNYS")
    _calendar(calendar_name)
    cutoff = _configured_minutes(ingestion.get("decision_cutoff_local", ""))
    if cutoff is None:
        raise ValueError("provider_ingestion.decision_cutoff_local must be configured")

    if not str(ingestion.get("database_path", "")).strip():
        raise ValueError("provider_ingestion.database_path must be configured")
    if str(ingestion.get("output_subdir", "")).strip() != "provider_ingestion":
        raise ValueError("provider_ingestion.output_subdir must be provider_ingestion")
    batch_size = _positive_number(
        ingestion.get("batch_size", 0),
        label="provider_ingestion.batch_size",
    )
    if not batch_size.is_integer():
        raise ValueError("provider_ingestion.batch_size must be an integer")
    _positive_number(
        ingestion.get("writer_lock_timeout_sec", 0),
        label="provider_ingestion.writer_lock_timeout_sec",
    )
    poll_minutes = _positive_number(
        ingestion.get("scheduler_poll_minutes", 0),
        label="provider_ingestion.scheduler_poll_minutes",
    )
    if not poll_minutes.is_integer():
        raise ValueError("provider_ingestion.scheduler_poll_minutes must be an integer")
    attempts = _positive_number(
        ingestion.get("max_scheduled_attempts", 0),
        label="provider_ingestion.max_scheduled_attempts",
    )
    if not attempts.is_integer() or attempts < 2:
        raise ValueError("provider_ingestion.max_scheduled_attempts must be an integer of at least two")
    capture_timeout = _positive_number(
        ingestion.get("capture_timeout_minutes", 0),
        label="provider_ingestion.capture_timeout_minutes",
    )
    if not capture_timeout.is_integer():
        raise ValueError("provider_ingestion.capture_timeout_minutes must be an integer")
    dispatch_stale = _positive_number(
        ingestion.get("dispatch_stale_minutes", 0),
        label="provider_ingestion.dispatch_stale_minutes",
    )
    if dispatch_stale <= capture_timeout:
        raise ValueError("provider_ingestion.dispatch_stale_minutes must exceed capture_timeout_minutes")

    schedules = ingestion.get("schedules")
    if not isinstance(schedules, Mapping):
        raise ValueError("provider_ingestion.schedules must be a mapping")
    configured_times: dict[str, tuple[int, int]] = {}
    for phase in SCHEDULED_PHASES:
        parsed = _configured_minutes(schedules.get(phase, ""))
        if parsed is None:
            raise ValueError(f"provider_ingestion.schedules.{phase} must be configured")
        configured_times[phase] = parsed
    if str(schedules.get("intraday", "")).strip().casefold() != "disabled":
        raise ValueError("provider_ingestion.schedules.intraday must remain disabled")
    intraday_order = tuple(configured_times[phase] for phase in ("premarket", "priority_refresh", "postclose"))
    if intraday_order != tuple(sorted(intraday_order)) or len(set(intraday_order)) != len(intraday_order):
        raise ValueError("Premarket, priority-refresh, and postclose schedules must be strictly increasing")
    if configured_times["priority_refresh"] >= cutoff or configured_times["postclose"] <= cutoff:
        raise ValueError("Priority refresh must precede, and postclose must follow, the decision cutoff")

    grace = ingestion.get("phase_grace_minutes")
    if not isinstance(grace, Mapping):
        raise ValueError("provider_ingestion.phase_grace_minutes must be a mapping")
    for phase in ("default", *SCHEDULED_PHASES):
        if phase not in grace:
            raise ValueError(f"provider_ingestion.phase_grace_minutes.{phase} is required")
        phase_grace_minutes(phase, grace)
    legacy_grace = int(ingestion.get("schedule_grace_minutes", -1))
    if legacy_grace != int(grace["default"]):
        raise ValueError("schedule_grace_minutes must equal phase_grace_minutes.default")

    acceptance = ingestion.get("provider_acceptance")
    if not isinstance(acceptance, Mapping):
        raise ValueError("provider_ingestion.provider_acceptance must be a mapping")
    _fraction(
        acceptance.get("minimum_clean_request_fraction"),
        label="provider_ingestion.provider_acceptance.minimum_clean_request_fraction",
    )
    availability = acceptance.get("minimum_available_request_fraction")
    if not isinstance(availability, Mapping):
        raise ValueError("minimum_available_request_fraction must be a mapping")
    for provider in EXPECTED_PROVIDERS:
        _fraction(
            availability.get(provider),
            label=f"minimum_available_request_fraction.{provider}",
        )

    recovery = ingestion.get("recovery")
    if not isinstance(recovery, Mapping):
        raise ValueError("provider_ingestion.recovery must be a mapping")
    if recovery.get("policy_version") != EXPECTED_RECOVERY_POLICY_VERSION:
        raise ValueError(f"provider_ingestion.recovery.policy_version must be {EXPECTED_RECOVERY_POLICY_VERSION}")
    date.fromisoformat(str(recovery.get("service_started_on", "")))
    for key in ("portfolio_catchup_lookback_sessions", "continuity_lookback_calendar_days"):
        parsed = _positive_number(recovery.get(key, 0), label=f"provider_ingestion.recovery.{key}")
        if not parsed.is_integer():
            raise ValueError(f"provider_ingestion.recovery.{key} must be an integer")
    expected_recovery = {
        "missed_capture_action": "flag_no_backfill",
        "universe_registry_policy": "provider_owned_append_only",
        "universe_source_refresh": "opportunistic_sealed_handoff",
        "universe_source_age_is_capture_warning": False,
    }
    actual_recovery = {key: recovery.get(key) for key in expected_recovery}
    if actual_recovery != expected_recovery:
        raise ValueError(f"provider_ingestion.recovery contract mismatch: {actual_recovery}")

    actionability_policy = ingestion.get("actionability")
    expected_actionability = {
        "require_full_cycle_before_same_session_cutoff": True,
        "weekend_observations_effective_next_session": True,
        "post_cutoff_observations_effective_next_session": True,
        "historical_current_snapshot_calls_prohibited": True,
    }
    if (
        not isinstance(actionability_policy, Mapping)
        or {key: actionability_policy.get(key) for key in expected_actionability} != expected_actionability
    ):
        raise ValueError("provider_ingestion.actionability contract does not match store semantics")


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


def latest_completed_session(calendar_name: str, *, now_utc: datetime) -> date:
    """Return the latest session whose exchange close has elapsed."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must include a timezone")
    current_utc = now_utc.astimezone(timezone.utc)
    calendar = _calendar(calendar_name)
    local_date = current_utc.astimezone(ZoneInfo(str(calendar.tz))).date()
    session = calendar.date_to_session(_timestamp(local_date), direction="previous")
    close_utc = calendar.session_close(session).to_pydatetime().astimezone(timezone.utc)
    if current_utc < close_utc:
        session = calendar.previous_session(session)
    return session.date()


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


def scheduled_capture_phases(value: date, *, calendar_name: str) -> tuple[str, ...]:
    if value.weekday() == 6:
        return ("sunday_baseline",)
    return (
        ("premarket", "priority_refresh", "postclose")
        if value in set(session_dates(calendar_name, value, value))
        else ()
    )


def phase_grace_minutes(phase: str, configured: int | Mapping[str, Any]) -> int:
    raw = configured.get(phase, configured.get("default", 0)) if isinstance(configured, Mapping) else configured
    value = int(raw)
    if value < 0:
        raise ValueError(f"Schedule grace must be non-negative: {phase}={value}")
    return value


def expected_capture_slots(
    *,
    start: date,
    end: date,
    now_utc: datetime,
    schedules: Mapping[str, Any],
    timezone_name: str,
    calendar_name: str,
    grace_minutes: int | Mapping[str, Any],
    service_started_on: date,
) -> list[dict[str, str]]:
    """Enumerate elapsed exchange-calendar slots; in-flight slots are excluded."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must include a timezone")
    effective_start = max(start, service_started_on)
    if end < effective_start:
        return []
    zone = ZoneInfo(timezone_name)
    output: list[dict[str, str]] = []
    cursor = effective_start
    while cursor <= end:
        for phase in scheduled_capture_phases(cursor, calendar_name=calendar_name):
            parsed = _configured_minutes(schedules.get(phase, "disabled"))
            if parsed is None:
                continue
            due_local = datetime.combine(cursor, time(*parsed), tzinfo=zone)
            due_utc = due_local.astimezone(timezone.utc)
            grace = phase_grace_minutes(phase, grace_minutes)
            if due_utc + timedelta(minutes=grace) > now_utc.astimezone(timezone.utc):
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


def _dispatch_table_exists(conn: sqlite3.Connection, table_prefix: str) -> bool:
    schema = table_prefix[:-1] if table_prefix else "main"
    if table_prefix not in {"", "provider_store."}:
        raise ValueError("Unsupported provider-store table prefix")
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='scheduled_dispatch_attempts'"
    ).fetchone()
    return row is not None


def capture_continuity_rows(
    conn: sqlite3.Connection,
    *,
    slots: Sequence[Mapping[str, str]],
    table_prefix: str = "",
) -> list[dict[str, Any]]:
    """Compare elapsed slots with scheduled-only capture and dispatch evidence."""
    if table_prefix and not table_prefix.endswith("."):
        raise ValueError("table_prefix must be empty or end with '.'")
    has_dispatches = _dispatch_table_exists(conn, table_prefix)
    output: list[dict[str, Any]] = []
    for slot in slots:
        by_cycle: dict[str, dict[str, Any]] = {}
        if has_dispatches:
            dispatches = conn.execute(
                f"SELECT cycle_id,state,started_at_utc,completed_at_utc "
                f"FROM {table_prefix}scheduled_dispatch_attempts "
                "WHERE actual_capture_date=? AND capture_phase=? "
                "ORDER BY started_at_utc,cycle_id",
                (slot["capture_date"], slot["capture_phase"]),
            ).fetchall()
            for row in dispatches:
                cycle_id = str(row["cycle_id"])
                by_cycle[cycle_id] = {
                    "cycle_id": cycle_id,
                    "state": str(row["state"]),
                    "started_at_utc": str(row["started_at_utc"]),
                    "completed_at_utc": str(row["completed_at_utc"]),
                    "accepted": False,
                }
        captures = conn.execute(
            f"SELECT cycle_id,status,started_at_utc,completed_at_utc "
            f"FROM {table_prefix}capture_runs "
            "WHERE actual_capture_date=? AND capture_phase=? "
            "AND cycle_id LIKE 'scheduled-%' "
            "ORDER BY completed_at_utc,cycle_id",
            (slot["capture_date"], slot["capture_phase"]),
        ).fetchall()
        for row in captures:
            cycle_id = str(row["cycle_id"])
            status = str(row["status"])
            dispatch = by_cycle.get(cycle_id)
            dispatch_state = "" if dispatch is None else str(dispatch["state"])
            dispatch_passed = not has_dispatches or dispatch_state == "PASS"
            by_cycle[cycle_id] = {
                "cycle_id": cycle_id,
                "state": (
                    status
                    if dispatch_passed
                    else dispatch_state
                    if dispatch_state
                    else "CAPTURE_WITHOUT_DISPATCH"
                ),
                "started_at_utc": (
                    str(row["started_at_utc"])
                    if dispatch is None
                    else str(dispatch["started_at_utc"])
                ),
                "completed_at_utc": (
                    str(row["completed_at_utc"])
                    if dispatch is None or not str(dispatch["completed_at_utc"])
                    else str(dispatch["completed_at_utc"])
                ),
                "accepted": status in ACCEPTED_CAPTURE_STATUSES and dispatch_passed,
            }
        attempts = sorted(
            by_cycle.values(),
            key=lambda row: (
                str(row["completed_at_utc"] or row["started_at_utc"]),
                str(row["cycle_id"]),
            ),
        )
        accepted = [row for row in attempts if bool(row["accepted"])]
        terminal_failed = [row for row in attempts if not row["accepted"] and str(row["state"]) != "STARTED"]
        started = [row for row in attempts if str(row["state"]) == "STARTED"]
        latest = attempts[-1] if attempts else None
        status = "PASS" if accepted else "FAILED" if terminal_failed else "IN_PROGRESS" if started else "MISSING"
        output.append(
            {
                **dict(slot),
                "status": status,
                "attempt_count": len(attempts),
                "accepted_attempt_count": len(accepted),
                "failed_attempt_count": len(terminal_failed),
                "latest_cycle_id": "" if latest is None else str(latest["cycle_id"]),
                "latest_completed_at_utc": ("" if latest is None else str(latest["completed_at_utc"])),
                "detail": (
                    "accepted scheduled capture exists"
                    if accepted
                    else "scheduled attempts did not produce an accepted capture"
                    if terminal_failed
                    else "scheduled capture is still recorded as started"
                    if started
                    else "elapsed scheduled slot has no dispatch; no backfill permitted"
                ),
            }
        )
    return output


def continuity_gaps(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("status")) != "PASS"]
