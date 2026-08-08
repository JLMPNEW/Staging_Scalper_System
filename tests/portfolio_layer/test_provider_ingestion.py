from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

import pytest
import yaml

from portfolio_layer.provider_ingestion.capture import _load_independent_universe
from portfolio_layer.provider_ingestion.health import (
    capture_continuity_rows,
    continuity_gaps,
    expected_capture_slots,
    universe_freshness,
)
from portfolio_layer.provider_ingestion.publish import _as_of_rows
from portfolio_layer.provider_ingestion.run_due import (
    _capture_manifest_errors,
    main as run_due_main,
    verified_summary_lines,
)
from portfolio_layer.provider_ingestion.store import (
    actionability,
    connect_store,
    freeze_universe,
    persist_capture,
    reject_historical_current_capture,
    verify_store,
)


def _summary_row(
    *,
    completed_at_utc: str,
    status: str = "PASS_WITH_WARNINGS",
    member_count: int = 65,
    request_count: int = 195,
    error_request_count: int = 0,
) -> dict[str, object]:
    return {
        "completed_at_utc": completed_at_utc,
        "status": status,
        "member_count": member_count,
        "request_count": request_count,
        "error_request_count": error_request_count,
    }


def test_verified_morning_summary_contract() -> None:
    lines, verified = verified_summary_lines(
        rows={
            "premarket": _summary_row(
                completed_at_utc="2026-08-04T11:36:40+00:00"
            ),
            "priority_refresh": _summary_row(
                completed_at_utc="2026-08-04T12:56:40+00:00"
            ),
        },
        expected_phases=("premarket", "priority_refresh"),
        required_phases=("premarket", "priority_refresh"),
        zone=ZoneInfo("America/New_York"),
        hash_errors=[],
        store_errors=[],
    )

    assert verified
    assert lines == [
        "**Independent provider ingestion ran successfully this morning:**",
        "Premarket: 7:37 AM ET, PASS_WITH_WARNINGS",
        "Priority refresh: 8:57 AM ET, PASS_WITH_WARNINGS",
        "65 names, 195 requests per cycle",
        "No request errors",
        "Output hashes: valid",
        "Provider database validation: PASS",
    ]


def test_provider_summary_does_not_claim_morning_complete_early() -> None:
    lines, verified = verified_summary_lines(
        rows={
            "premarket": _summary_row(
                completed_at_utc="2026-08-04T11:37:00+00:00"
            )
        },
        expected_phases=("premarket", "priority_refresh"),
        required_phases=("premarket",),
        zone=ZoneInfo("America/New_York"),
        hash_errors=[],
        store_errors=[],
    )

    assert verified
    assert lines[0] == "**Independent provider ingestion ran successfully:**"
    assert "Priority refresh: PENDING" in lines


def test_provider_summary_missed_required_phase_fails() -> None:
    lines, verified = verified_summary_lines(
        rows={
            "premarket": _summary_row(
                completed_at_utc="2026-08-04T11:37:00+00:00"
            )
        },
        expected_phases=("premarket", "priority_refresh"),
        required_phases=("premarket", "priority_refresh"),
        zone=ZoneInfo("America/New_York"),
        hash_errors=[],
        store_errors=[],
    )

    assert not verified
    assert lines[0] == "**Independent provider ingestion incomplete:**"
    assert "Priority refresh: MISSING" in lines
    assert not any("successfully" in line for line in lines)


def _write_cycle_manifest(
    output_root: Path,
    *,
    cycle_id: str,
    capture_phase: str,
    actual_capture_date: str,
    status: str,
) -> Path:
    cycle_dir = output_root / cycle_id
    cycle_dir.mkdir(parents=True)
    output = cycle_dir / "capture_requests.csv"
    output.write_text("ticker,status\nAAA,AVAILABLE\n", encoding="utf-8")
    manifest = {
        "acceptance": status,
        "actual_capture_date": actual_capture_date,
        "capture_phase": capture_phase,
        "cycle_id": cycle_id,
        "outputs_sha256": {
            "capture_requests.csv": hashlib.sha256(output.read_bytes()).hexdigest()
        },
    }
    (cycle_dir / "capture_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return output


def _setup_run_due_env(tmp_path: Path) -> Path:
    """Create a config, provider store with a completed 2026-08-03 morning, and
    matching capture manifests under tmp_path; return the config path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {"database_path": "db/portfolio_layer.sqlite", "output_dir": "output"},
                "provider_ingestion": {
                    "database_path": "db/provider_observations.sqlite",
                    "output_subdir": "provider_ingestion",
                    "timezone": "America/New_York",
                    "missed_run_policy": "current_only_no_backfill",
                    "schedule_grace_minutes": 20,
                    "max_scheduled_attempts": 2,
                    "writer_lock_timeout_sec": 5.0,
                    "schedules": {
                        "sunday_baseline": "18:00",
                        "premarket": "07:30",
                        "priority_refresh": "08:45",
                        "intraday": "disabled",
                        "postclose": "disabled",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "output" / "provider_ingestion"
    conn = connect_store(tmp_path / "db" / "provider_observations.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-03",
            capture_phase="premarket",
            members=[{"ticker": "AAA", "tier": "tier0", "sector": "", "source_pipeline": "test"}],
            providers=["fmp"],
            created_at_utc="2026-08-03T11:30:00+00:00",
        )
        for phase, completed in (
            ("premarket", "2026-08-03T11:36:40+00:00"),
            ("priority_refresh", "2026-08-03T12:56:40+00:00"),
        ):
            cycle_id = f"scheduled-20260803-{phase}-a01"
            persist_capture(
                conn,
                cycle_id=cycle_id,
                capture_phase=phase,
                requested_portfolio_as_of="2026-08-03",
                actual_capture_date="2026-08-03",
                universe_id=universe_id,
                started_at_utc=completed,
                completed_at_utc=completed,
                request_records=[_request(received=completed, average=2.0)],
                source_code_digest="b" * 64,
                config_digest="c" * 64,
                timezone_name="America/New_York",
                calendar_name="XNYS",
                decision_cutoff_local="09:25",
                status="PASS",
            )
            _write_cycle_manifest(
                output_root,
                cycle_id=cycle_id,
                capture_phase=phase,
                actual_capture_date="2026-08-03",
                status="PASS",
            )
    finally:
        conn.close()
    return config_path


def _run_due_main(
    monkeypatch: pytest.MonkeyPatch, *, config_path: Path, now_utc: str
) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_due.py", "--config", str(config_path), "--now-utc", now_utc],
    )
    return run_due_main()


def test_run_due_already_complete_in_grace_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    exit_code = _run_due_main(
        monkeypatch, config_path=config_path, now_utc="2026-08-03T12:58:00+00:00"
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "priority_refresh already completed for 2026-08-03" in out
    assert "**Independent provider ingestion ran successfully this morning:**" in out
    assert "Premarket: 7:37 AM ET, PASS" in out
    assert "Priority refresh: 8:57 AM ET, PASS" in out
    assert "Output hashes: valid" in out
    assert "Provider database validation: PASS" in out
    log_text = (
        tmp_path / "output" / "provider_ingestion" / "run_due_summary.log"
    ).read_text(encoding="utf-8")
    assert "ran successfully this morning" in log_text


def test_run_due_no_phase_due_still_prints_completed_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    exit_code = _run_due_main(
        monkeypatch, config_path=config_path, now_utc="2026-08-03T14:00:00+00:00"
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no current capture is due" in out
    assert "**Independent provider ingestion ran successfully this morning:**" in out
    assert "1 names, 1 requests per cycle" in out
    assert "No request errors" in out


def test_run_due_pending_phases_do_not_claim_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    exit_code = _run_due_main(
        monkeypatch, config_path=config_path, now_utc="2026-08-04T10:00:00+00:00"
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "**Independent provider ingestion pending:**" in out
    assert "Premarket: PENDING" in out
    assert "ran successfully" not in out


def test_run_due_validation_failure_exits_nonzero_without_success_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)
    tampered = (
        tmp_path
        / "output"
        / "provider_ingestion"
        / "scheduled-20260803-premarket-a01"
        / "capture_requests.csv"
    )
    tampered.write_text("ticker,status\nAAA,EMPTY\n", encoding="utf-8")

    exit_code = _run_due_main(
        monkeypatch, config_path=config_path, now_utc="2026-08-03T14:00:00+00:00"
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "**Independent provider ingestion verification failed:**" in out
    assert "Output hashes: INVALID" in out
    assert "manifest_output_hash_mismatch" in out
    assert "ran successfully" not in out
    log_text = (
        tmp_path / "output" / "provider_ingestion" / "run_due_summary.log"
    ).read_text(encoding="utf-8")
    assert "verification=FAIL" in log_text


def test_run_due_missed_elapsed_phase_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    # Tuesday 10:00 ET: both morning phases elapsed, neither captured that day.
    exit_code = _run_due_main(
        monkeypatch, config_path=config_path, now_utc="2026-08-04T14:00:00+00:00"
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "**Independent provider ingestion incomplete:**" in out
    assert "Premarket: MISSING" in out
    assert "ran successfully" not in out


def test_capture_manifest_output_hash_is_verified(tmp_path: Path) -> None:
    cycle_id = "scheduled-20260804-priority_refresh-a01"
    cycle_dir = tmp_path / cycle_id
    cycle_dir.mkdir()
    output = cycle_dir / "capture_requests.csv"
    output.write_text("ticker,status\nAAA,AVAILABLE\n", encoding="utf-8")
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        "acceptance": "PASS_WITH_WARNINGS",
        "actual_capture_date": "2026-08-04",
        "capture_phase": "priority_refresh",
        "cycle_id": cycle_id,
        "outputs_sha256": {"capture_requests.csv": output_sha},
    }
    (cycle_dir / "capture_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    row = {
        "acceptance": "PASS_WITH_WARNINGS",
        "actual_capture_date": "2026-08-04",
        "capture_phase": "priority_refresh",
        "cycle_id": cycle_id,
        "status": "PASS_WITH_WARNINGS",
    }

    assert _capture_manifest_errors(row, output_root=tmp_path) == []
    output.write_text("ticker,status\nAAA,EMPTY\n", encoding="utf-8")
    assert _capture_manifest_errors(row, output_root=tmp_path) == [
        f"manifest_output_hash_mismatch:{cycle_id}:capture_requests.csv"
    ]


def test_provider_service_start_is_quoted_iso_date() -> None:
    config_path = Path(__file__).resolve().parents[2] / "portfolio_layer" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    service_started_on = config["provider_ingestion"]["recovery"]["service_started_on"]

    assert isinstance(service_started_on, str)
    date.fromisoformat(service_started_on)


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


def test_provider_universe_registry_survives_monitor_and_pipeline_outage(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    universe_dir = output_root / "runs" / "2026-08-03" / "expectations_monitor"
    universe_dir.mkdir(parents=True)
    universe_csv = universe_dir / "monitor_universe.csv"
    universe_csv.write_text(
        """ticker,tier,sector,source_pipeline
AAA,tier0,Technology,test
BBB,tier1,Industrials,test
""",
        encoding="utf-8",
    )
    universe_sha = hashlib.sha256(universe_csv.read_bytes()).hexdigest()
    (universe_dir / "monitor_universe_manifest.json").write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "run_as_of": "2026-08-03",
                "outputs_sha256": {"monitor_universe.csv": universe_sha},
            }
        ),
        encoding="utf-8",
    )
    store_path = tmp_path / "provider.sqlite"

    source_as_of, members, health = _load_independent_universe(
        store_path=store_path,
        output_root=output_root,
        output_subdir="expectations_monitor",
        phase="premarket",
        actual_date=date(2026, 8, 4),
        timeout_sec=5.0,
        providers=["fmp"],
    )
    assert source_as_of == "2026-08-03"
    assert [row["ticker"] for row in members] == ["AAA"]
    assert health["status"] == "ACTIVE_REGISTRY"
    assert health["capture_independent"] is True
    assert health["sync_status"] == "INITIALIZED"

    source_as_of, members, health = _load_independent_universe(
        store_path=store_path,
        output_root=tmp_path / "monitor-and-pipeline-offline",
        output_subdir="expectations_monitor",
        phase="premarket",
        actual_date=date(2026, 8, 5),
        timeout_sec=5.0,
        providers=["fmp"],
    )
    assert source_as_of == "2026-08-03"
    assert [row["ticker"] for row in members] == ["AAA"]
    assert health["status"] == "ACTIVE_REGISTRY"
    assert health["sync_status"] == "NO_NEW_HANDOFF"
    assert health["sync_diagnostics"] == ["monitor_runs_root_missing"]

    conn = connect_store(store_path)
    try:
        assert verify_store(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM provider_universe_registry"
        ).fetchone()[0] == 1
    finally:
        conn.close()


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
