from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from portfolio_layer.provider_ingestion.health import (
    capture_continuity_rows,
    continuity_gaps,
    expected_capture_slots,
    universe_freshness,
)
from portfolio_layer.provider_ingestion.publish import _as_of_rows
from portfolio_layer.provider_ingestion.store import (
    actionability,
    connect_store,
    freeze_universe,
    persist_capture,
    reject_historical_current_capture,
    verify_store,
)


def _request(*, received: str, average: float) -> dict[str, object]:
    return {
        "provider": "fmp",
        "endpoint_id": "analyst_estimates",
        "ticker": "AAA",
        "provider_symbol": "AAA",
        "status": "AVAILABLE",
        "http_status": 200,
        "elapsed_ms": 10,
        "provider_row_count": 1,
        "request_started_at_utc": received,
        "response_received_at_utc": received,
        "response_sha256": "a" * 64,
        "detail": "ok",
        "normalized_rows": [
            {
                "provider": "fmp",
                "endpoint_id": "analyst_estimates",
                "ticker": "AAA",
                "fiscal_period_end": "2026-12-31",
                "fiscal_period": "annual",
                "estimate_type": "eps_annual",
                "currency": "USD",
                "estimate_average": average,
                "estimate_high": average + 0.2,
                "estimate_low": average - 0.2,
                "analyst_count": 10,
            }
        ],
    }


def _persist(
    conn: sqlite3.Connection, *, cycle: str, received: str, average: float, universe_id: str
) -> dict[str, object]:
    return persist_capture(
        conn,
        cycle_id=cycle,
        capture_phase="premarket",
        requested_portfolio_as_of="2026-08-03",
        actual_capture_date="2026-08-03",
        universe_id=universe_id,
        started_at_utc=received,
        completed_at_utc=received,
        request_records=[_request(received=received, average=average)],
        source_code_digest="b" * 64,
        config_digest="c" * 64,
        timezone_name="America/New_York",
        calendar_name="XNYS",
        decision_cutoff_local="09:25",
        status="PASS",
    )


def test_exchange_calendar_actionability() -> None:
    sunday = actionability(
        response_received_at_utc="2026-08-02T15:00:00+00:00",
        cycle_completed_at_utc="2026-08-02T15:01:00+00:00",
        timezone_name="America/New_York",
        calendar_name="XNYS",
        decision_cutoff_local="09:25",
    )
    assert sunday["effective_trading_date"] == "2026-08-03"
    assert sunday["same_session_eligible"] == 0
    premarket = actionability(
        response_received_at_utc="2026-08-03T12:00:00+00:00",
        cycle_completed_at_utc="2026-08-03T12:05:00+00:00",
        timezone_name="America/New_York",
        calendar_name="XNYS",
        decision_cutoff_local="09:25",
    )
    assert premarket["effective_trading_date"] == "2026-08-03"
    assert premarket["same_session_eligible"] == 1
    late = actionability(
        response_received_at_utc="2026-08-03T14:00:00+00:00",
        cycle_completed_at_utc="2026-08-03T14:01:00+00:00",
        timezone_name="America/New_York",
        calendar_name="XNYS",
        decision_cutoff_local="09:25",
    )
    assert late["effective_trading_date"] == "2026-08-04"
    assert late["same_session_eligible"] == 0
    crossed_cutoff = actionability(
        response_received_at_utc="2026-08-03T13:20:00+00:00",
        cycle_completed_at_utc="2026-08-03T13:26:00+00:00",
        timezone_name="America/New_York",
        calendar_name="XNYS",
        decision_cutoff_local="09:25",
    )
    assert crossed_cutoff["effective_trading_date"] == "2026-08-04"
    assert crossed_cutoff["same_session_eligible"] == 0


def test_schedule_continuity_and_universe_freshness(tmp_path: Path) -> None:
    schedules = {
        "sunday_baseline": "18:00",
        "premarket": "07:30",
        "priority_refresh": "08:45",
        "postclose": "18:00",
    }
    slots = expected_capture_slots(
        start=date(2026, 8, 2),
        end=date(2026, 8, 3),
        now_utc=datetime(2026, 8, 3, 13, 10, tzinfo=timezone.utc),
        schedules=schedules,
        timezone_name="America/New_York",
        calendar_name="XNYS",
        grace_minutes=20,
        service_started_on=date(2026, 8, 2),
    )
    assert [(row["capture_date"], row["capture_phase"]) for row in slots] == [
        ("2026-08-02", "sunday_baseline"),
        ("2026-08-03", "premarket"),
        ("2026-08-03", "priority_refresh"),
    ]
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        rows = capture_continuity_rows(conn, slots=slots)
    finally:
        conn.close()
    assert len(continuity_gaps(rows)) == 3
    assert all(row["status"] == "MISSING" for row in rows)

    fresh = universe_freshness(
        "XNYS",
        actual_date=date(2026, 8, 3),
        phase="priority_refresh",
        universe_as_of="2026-07-31",
    )
    stale = universe_freshness(
        "XNYS",
        actual_date=date(2026, 8, 3),
        phase="priority_refresh",
        universe_as_of="2026-07-30",
    )
    postclose = universe_freshness(
        "XNYS",
        actual_date=date(2026, 8, 3),
        phase="postclose",
        universe_as_of="2026-08-03",
    )
    assert fresh["status"] == "CURRENT"
    assert stale == {
        "status": "STALE",
        "universe_as_of": "2026-07-30",
        "expected_universe_as_of": "2026-07-31",
        "lag_sessions": 1,
    }
    assert postclose["status"] == "CURRENT"


def test_current_endpoint_rejects_historical_date() -> None:
    with pytest.raises(ValueError, match="cannot be queried"):
        reject_historical_current_capture(
            requested_portfolio_as_of=date(2026, 7, 31),
            now_utc=datetime(2026, 8, 2, 15, tzinfo=timezone.utc),
            timezone_name="America/New_York",
        )


def test_versions_are_deduplicated_but_checks_are_retained(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-03",
            capture_phase="premarket",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "Technology",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-03T12:00:00+00:00",
        )
        first = _persist(
            conn,
            cycle="cycle-1",
            received="2026-08-03T12:00:00+00:00",
            average=2.0,
            universe_id=universe_id,
        )
        second = _persist(
            conn,
            cycle="cycle-2",
            received="2026-08-04T12:00:00+00:00",
            average=2.0,
            universe_id=universe_id,
        )
        third = _persist(
            conn,
            cycle="cycle-3",
            received="2026-08-05T12:00:00+00:00",
            average=2.2,
            universe_id=universe_id,
        )
        assert first["new_version_count"] == 1
        assert second["new_version_count"] == 0
        assert second["unchanged_observation_count"] == 1
        assert third["new_version_count"] == 1
        assert conn.execute("SELECT COUNT(*) FROM estimate_versions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM estimate_observations").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM estimate_changes").fetchone()[0] == 1
        assert verify_store(conn) == []
        assert _persist(
            conn,
            cycle="cycle-3",
            received="2026-08-05T12:00:00+00:00",
            average=2.2,
            universe_id=universe_id,
        )["idempotent"] is True
        with pytest.raises(ValueError, match="different request content"):
            _persist(
                conn,
                cycle="cycle-3",
                received="2026-08-05T12:00:00+00:00",
                average=9.9,
                universe_id=universe_id,
            )
        request_id = conn.execute(
            "SELECT request_id FROM capture_requests ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        with conn:
            conn.execute(
                "UPDATE capture_requests SET detail='tampered' WHERE request_id=?",
                (request_id,),
            )
        assert any(error.startswith("run_digest_mismatch:") for error in verify_store(conn))
    finally:
        conn.close()


def test_asof_publication_respects_effective_session(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-02",
            capture_phase="sunday_baseline",
            members=[{"ticker": "AAA", "tier": "tier0", "sector": "", "source_pipeline": "test"}],
            providers=["fmp"],
            created_at_utc="2026-08-02T15:00:00+00:00",
        )
        persist_capture(
            conn,
            cycle_id="sunday",
            capture_phase="sunday_baseline",
            requested_portfolio_as_of="2026-08-02",
            actual_capture_date="2026-08-02",
            universe_id=universe_id,
            started_at_utc="2026-08-02T15:00:00+00:00",
            completed_at_utc="2026-08-02T15:01:00+00:00",
            request_records=[_request(received="2026-08-02T15:00:00+00:00", average=2.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
        )
        assert _as_of_rows(
            conn,
            as_of=date(2026, 8, 2),
            cutoff_utc=datetime(2026, 8, 3, 4, tzinfo=timezone.utc),
        ) == []
        assert len(
            _as_of_rows(
                conn,
                as_of=date(2026, 8, 3),
                cutoff_utc=datetime(2026, 8, 4, 4, tzinfo=timezone.utc),
            )
        ) == 1
    finally:
        conn.close()


def test_failed_capture_is_auditable_but_not_consumable(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-03",
            capture_phase="premarket",
            members=[{"ticker": "AAA", "tier": "tier0", "sector": "", "source_pipeline": "test"}],
            providers=["fmp"],
            created_at_utc="2026-08-03T12:00:00+00:00",
        )
        persist_capture(
            conn,
            cycle_id="failed-cycle",
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-03",
            actual_capture_date="2026-08-03",
            universe_id=universe_id,
            started_at_utc="2026-08-03T12:00:00+00:00",
            completed_at_utc="2026-08-03T12:01:00+00:00",
            request_records=[_request(received="2026-08-03T12:00:00+00:00", average=2.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="FAIL",
        )
        assert conn.execute("SELECT COUNT(*) FROM estimate_observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_estimate_snapshots").fetchone()[0] == 0
        assert verify_store(conn) == []
    finally:
        conn.close()
