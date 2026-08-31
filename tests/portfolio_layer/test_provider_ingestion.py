from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
import yaml

from portfolio_layer.core.contracts import write_csv
from portfolio_layer.provider_ingestion.artifacts import (
    REPORT_FIELDS,
    REPORT_ORDER_SCHEMA,
    capture_report_order,
    capture_report_rows,
    ensure_capture_manifest,
)
from portfolio_layer.provider_ingestion.capture import (
    _capture_request,
    _load_independent_universe,
    _phase_tiers,
    _payload_kind_matches,
    _provider_acceptance,
    _validated_cycle_id,
    _valid_response_sha256,
    _validated_normalized_rows,
)
from portfolio_layer.expectations_monitor.estimate_normalization import normalize_estimates
from portfolio_layer.expectations_monitor.provider_common import (
    ProviderPayloadResult,
    classify_payload,
)
from portfolio_layer.provider_ingestion.health import (
    capture_continuity_rows,
    continuity_gaps,
    scheduled_capture_phases,
    expected_capture_slots,
    latest_completed_session,
    universe_freshness,
    validate_provider_ingestion_policy,
)
from portfolio_layer.provider_ingestion.publish import (
    _as_of_rows,
    _validated_cutoff,
    main as publish_main,
)
from portfolio_layer.provider_ingestion.recover import main as recover_main
from portfolio_layer.provider_ingestion.validate import main as validate_main
from portfolio_layer.provider_ingestion.run_due import (
    _capture_manifest_errors,
    _next_attempt_number,
    due_phases,
    _scheduled_attempt_ids,
    _write_scheduler_attempt,
    main as run_due_main,
    verified_summary_lines,
    write_summary_log,
)
from portfolio_layer.provider_ingestion.store import (
    actionability,
    connect_store,
    connect_store_readonly,
    freeze_universe,
    finalize_dispatch_attempt,
    interrupt_stale_dispatch_attempts,
    record_dispatch_started,
    persist_capture,
    reject_historical_current_capture,
    require_scheduled_dispatch,
    verify_store,
    verify_store_head,
    writer_lock,
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
            "premarket": _summary_row(completed_at_utc="2026-08-04T11:36:40+00:00"),
            "priority_refresh": _summary_row(completed_at_utc="2026-08-04T12:56:40+00:00"),
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


def test_writer_lock_recovers_after_process_crash(tmp_path: Path) -> None:
    lock_path = tmp_path / "provider.capture.lock"
    project_root = Path(__file__).resolve().parents[2]
    child_code = "\n".join(
        (
            "import os, sys",
            "from pathlib import Path",
            "from portfolio_layer.provider_ingestion.store import writer_lock",
            "with writer_lock(Path(sys.argv[1]), timeout_sec=1.0):",
            "    print('locked', flush=True)",
            "    os._exit(0)",
        )
    )
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(lock_path)],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "locked"
    interrupted = json.loads(lock_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "active"

    with writer_lock(lock_path, timeout_sec=1.0):
        pass
    released = json.loads(lock_path.read_text(encoding="utf-8"))
    assert released["state"] == "released"
    assert released["pid"] == os.getpid()


def test_store_head_validation_checks_latest_dispatch_artifact(tmp_path: Path) -> None:
    store_path = tmp_path / "provider.sqlite"
    artifact_path = (tmp_path / "scheduler_attempt.json").resolve()
    artifact_path.write_text("{}\n", encoding="utf-8")
    cycle_id = "scheduled-20260828-postclose-a01"
    conn = connect_store(store_path)
    try:
        record_dispatch_started(
            conn,
            cycle_id=cycle_id,
            actual_capture_date="2026-08-28",
            capture_phase="postclose",
            started_at_utc="2026-08-28T22:00:00+00:00",
            parent_pid=123,
        )
        finalize_dispatch_attempt(
            conn,
            cycle_id=cycle_id,
            completed_at_utc="2026-08-28T22:01:00+00:00",
            state="PASS",
            return_code=0,
            artifact_path=str(artifact_path),
            artifact_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            detail="test pass",
        )
        assert verify_store_head(conn) == []
        artifact_path.write_text("{\"tampered\":true}\n", encoding="utf-8")
        assert verify_store_head(conn) == [f"dispatch_artifact_hash_mismatch:{cycle_id}"]
    finally:
        conn.close()


def test_failed_scheduler_attempt_is_durable_and_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "do-not-persist-this-secret")
    output_root = tmp_path / "provider_ingestion"
    cycle_id = "scheduled-20260813-premarket-a01"
    completed = subprocess.CompletedProcess(
        args=["capture.py"],
        returncode=7,
        stdout="request failed: do-not-persist-this-secret\n",
        stderr="provider child failed\n",
    )
    manifest_path = _write_scheduler_attempt(
        output_root=output_root,
        cycle_id=cycle_id,
        command=["python", "capture.py"],
        completed=completed,
        invoked_at_utc=datetime(2026, 8, 13, 11, 33, tzinfo=timezone.utc),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["acceptance"] == "FAIL"
    assert manifest["returncode"] == 7
    log_text = (manifest_path.parent / "scheduler_child.log").read_text(encoding="utf-8")
    assert "do-not-persist-this-secret" not in log_text
    assert "[REDACTED]" in log_text
    assert _scheduled_attempt_ids(
        output_root=output_root,
        local_date="2026-08-13",
        phase="premarket",
        database_cycle_ids={"scheduled-20260813-premarket-a02"},
    ) == {cycle_id, "scheduled-20260813-premarket-a02"}


def test_provider_summary_does_not_claim_morning_complete_early() -> None:
    lines, verified = verified_summary_lines(
        rows={"premarket": _summary_row(completed_at_utc="2026-08-04T11:37:00+00:00")},
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
        rows={"premarket": _summary_row(completed_at_utc="2026-08-04T11:37:00+00:00")},
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
    run_row: dict[str, object],
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
        "raw_payloads_retained": False,
        "store_result": {
            field: run_row[field]
            for field in (
                "run_id",
                "run_digest",
                "status",
                "request_count",
                "normalized_row_count",
                "new_version_count",
                "unchanged_observation_count",
                "previous_pass_digest",
            )
        },
        "outputs_sha256": {"capture_requests.csv": hashlib.sha256(output.read_bytes()).hexdigest()},
    }
    (cycle_dir / "capture_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return output


def _setup_run_due_env(tmp_path: Path) -> Path:
    """Create a config, provider store with a completed 2026-08-03 morning, and
    matching capture manifests under tmp_path; return the config path."""
    config_path = tmp_path / "config.yaml"
    source_config_path = Path(__file__).resolve().parents[2] / "portfolio_layer" / "config.yaml"
    source_config = yaml.safe_load(source_config_path.read_text(encoding="utf-8"))
    ingestion = dict(source_config["provider_ingestion"])
    ingestion.update(
        {
            "database_path": "db/provider_observations.sqlite",
            "output_subdir": "provider_ingestion",
            "writer_lock_timeout_sec": 5.0,
        }
    )
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {"database_path": "db/portfolio_layer.sqlite", "output_dir": "output"},
                "provider_ingestion": ingestion,
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
            run_row = conn.execute("SELECT * FROM capture_runs WHERE cycle_id=?", (cycle_id,)).fetchone()
            assert run_row is not None
            _write_cycle_manifest(
                output_root,
                cycle_id=cycle_id,
                capture_phase=phase,
                actual_capture_date="2026-08-03",
                status="PASS",
                run_row=dict(run_row),
            )
            record_dispatch_started(
                conn,
                cycle_id=cycle_id,
                actual_capture_date="2026-08-03",
                capture_phase=phase,
                started_at_utc=completed,
                parent_pid=123,
            )
            attempt_path = output_root / cycle_id / "scheduler_attempt.json"
            attempt_path.write_text("{}\n", encoding="utf-8")
            finalize_dispatch_attempt(
                conn,
                cycle_id=cycle_id,
                completed_at_utc=completed,
                state="PASS",
                return_code=0,
                artifact_path=str(attempt_path.resolve()),
                artifact_sha256=hashlib.sha256(attempt_path.read_bytes()).hexdigest(),
                detail="test pass",
            )
    finally:
        conn.close()
    return config_path


def _run_due_main(monkeypatch: pytest.MonkeyPatch, *, config_path: Path, now_utc: str) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_due.py", "--config", str(config_path), "--now-utc", now_utc, "--dry-run"],
    )
    return run_due_main()


def test_run_due_rejects_simulated_clock_for_live_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path(__file__).resolve().parents[2] / "portfolio_layer" / "config.yaml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_due.py",
            "--config",
            str(config_path),
            "--now-utc",
            "2026-08-13T12:00:00+00:00",
        ],
    )
    with pytest.raises(ValueError, match="simulated clock.*--dry-run"):
        run_due_main()


def test_run_due_already_complete_in_grace_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    exit_code = _run_due_main(monkeypatch, config_path=config_path, now_utc="2026-08-03T12:58:00+00:00")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "all due phases already completed for 2026-08-03" in out
    assert "**Independent provider ingestion ran successfully this morning:**" in out
    assert "Premarket: 7:37 AM ET, PASS" in out
    assert "Priority refresh: 8:57 AM ET, PASS" in out
    assert "Output hashes: valid" in out
    assert "Provider database validation: PASS" in out
    assert not (tmp_path / "output" / "provider_ingestion" / "run_due_summary.log").exists()


def test_run_due_no_phase_due_still_prints_completed_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    exit_code = _run_due_main(monkeypatch, config_path=config_path, now_utc="2026-08-03T14:00:00+00:00")

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

    exit_code = _run_due_main(monkeypatch, config_path=config_path, now_utc="2026-08-04T10:00:00+00:00")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "**Independent provider ingestion pending:**" in out
    assert "Premarket: PENDING" in out
    assert "ran successfully" not in out


def test_run_due_validation_failure_exits_nonzero_without_success_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)
    tampered = tmp_path / "output" / "provider_ingestion" / "scheduled-20260803-premarket-a01" / "capture_requests.csv"
    tampered.write_text("ticker,status\nAAA,EMPTY\n", encoding="utf-8")

    exit_code = _run_due_main(monkeypatch, config_path=config_path, now_utc="2026-08-03T14:00:00+00:00")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "**Independent provider ingestion verification failed:**" in out
    assert "Output hashes: INVALID" in out
    assert "manifest_output_hash_mismatch" in out
    assert "ran successfully" not in out
    assert not (tmp_path / "output" / "provider_ingestion" / "run_due_summary.log").exists()


def test_run_due_missed_elapsed_phase_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _setup_run_due_env(tmp_path)

    # Tuesday 10:00 ET: both morning phases elapsed, neither captured that day.
    exit_code = _run_due_main(monkeypatch, config_path=config_path, now_utc="2026-08-04T14:00:00+00:00")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "**Independent provider ingestion incomplete:**" in out
    assert "Premarket: MISSING" in out
    assert "ran successfully" not in out


def test_capture_manifest_output_hash_is_verified(tmp_path: Path) -> None:
    cycle_id = "scheduled-20260804-priority_refresh-a01"
    row: dict[str, object] = {
        "run_id": "run-1",
        "run_digest": "d" * 64,
        "acceptance": "PASS_WITH_WARNINGS",
        "actual_capture_date": "2026-08-04",
        "capture_phase": "priority_refresh",
        "cycle_id": cycle_id,
        "status": "PASS_WITH_WARNINGS",
        "request_count": 1,
        "available_request_count": 1,
        "empty_request_count": 0,
        "error_request_count": 0,
        "normalized_row_count": 1,
        "new_version_count": 1,
        "unchanged_observation_count": 0,
        "previous_pass_digest": "0" * 64,
    }
    output = _write_cycle_manifest(
        tmp_path,
        cycle_id=cycle_id,
        capture_phase="priority_refresh",
        actual_capture_date="2026-08-04",
        status="PASS_WITH_WARNINGS",
        run_row=row,
    )

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
    assert latest_completed_session("XNYS", now_utc=datetime(2026, 8, 3, 19, 0, tzinfo=timezone.utc)) == date(
        2026, 7, 31
    )
    assert latest_completed_session("XNYS", now_utc=datetime(2026, 8, 3, 20, 1, tzinfo=timezone.utc)) == date(
        2026, 8, 3
    )
    assert latest_completed_session("XNYS", now_utc=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)) == date(2026, 8, 3)
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
        assert conn.execute("SELECT COUNT(*) FROM provider_universe_registry").fetchone()[0] == 1
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
        entitlement = conn.execute("SELECT DISTINCT entitlement_version FROM provider_estimate_snapshots").fetchone()[0]
        assert entitlement == "provider_entitlements_v1:provisional_retention_v1"
        assert conn.execute("SELECT COUNT(*) FROM estimate_changes").fetchone()[0] == 1
        assert verify_store(conn) == []
        assert (
            _persist(
                conn,
                cycle="cycle-3",
                received="2026-08-05T12:00:00+00:00",
                average=2.2,
                universe_id=universe_id,
            )["idempotent"]
            is True
        )
        with pytest.raises(ValueError, match="different request content"):
            _persist(
                conn,
                cycle="cycle-3",
                received="2026-08-05T12:00:00+00:00",
                average=9.9,
                universe_id=universe_id,
            )
        with pytest.raises(ValueError, match="different identity"):
            persist_capture(
                conn,
                cycle_id="cycle-3",
                capture_phase="postclose",
                requested_portfolio_as_of="2026-08-03",
                actual_capture_date="2026-08-03",
                universe_id=universe_id,
                started_at_utc="2026-08-05T12:00:00+00:00",
                completed_at_utc="2026-08-05T12:00:00+00:00",
                request_records=[_request(received="2026-08-05T12:00:00+00:00", average=2.2)],
                source_code_digest="b" * 64,
                config_digest="c" * 64,
                timezone_name="America/New_York",
                calendar_name="XNYS",
                decision_cutoff_local="09:25",
                status="PASS",
            )
        request_id = conn.execute("SELECT request_id FROM capture_requests ORDER BY rowid DESC LIMIT 1").fetchone()[0]
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
        assert (
            _as_of_rows(
                conn,
                as_of=date(2026, 8, 2),
                cutoff_utc=datetime(2026, 8, 3, 4, tzinfo=timezone.utc),
            )
            == []
        )
        assert (
            _as_of_rows(
                conn,
                as_of=date(2026, 8, 3),
                cutoff_utc=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
            )
            == []
        )
        assert (
            len(
                _as_of_rows(
                    conn,
                    as_of=date(2026, 8, 3),
                    cutoff_utc=datetime(2026, 8, 3, 14, tzinfo=timezone.utc),
                )
            )
            == 1
        )
    finally:
        conn.close()


def test_current_day_publication_cutoff_is_never_future_dated() -> None:
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    assert (
        _validated_cutoff(
            as_of=date(2026, 8, 3),
            timezone_name="America/New_York",
            requested=None,
            now_utc=now,
        )
        == now
    )
    with pytest.raises(ValueError, match="cannot be in the future"):
        _validated_cutoff(
            as_of=date(2026, 8, 3),
            timezone_name="America/New_York",
            requested=datetime(2026, 8, 3, 13, tzinfo=timezone.utc),
            now_utc=now,
        )
    with pytest.raises(ValueError, match="include a timezone"):
        _validated_cutoff(
            as_of=date(2026, 8, 3),
            timezone_name="America/New_York",
            requested=None,
            now_utc=datetime(2026, 8, 3, 12),
        )


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


def test_exchange_holidays_are_not_scheduled_and_week_end_tier_is_calendar_aware() -> None:
    holiday = date(2026, 7, 3)
    assert scheduled_capture_phases(holiday, calendar_name="XNYS") == ()
    assert (
        due_phases(
            now_local=datetime(2026, 7, 3, 8, 0, tzinfo=ZoneInfo("America/New_York")),
            schedules={
                "premarket": "07:30",
                "priority_refresh": "08:45",
                "postclose": "18:00",
            },
            grace_minutes=240,
            calendar_name="XNYS",
        )
        == ()
    )
    assert _phase_tiers(
        "postclose",
        actual_date=date(2026, 4, 2),
        calendar_name="XNYS",
    ) == {"tier0", "tier1", "tier2"}


def test_provider_acceptance_rejects_broad_outage_without_rejecting_partial_empty() -> None:
    policy = {
        "minimum_clean_request_fraction": 0.90,
        "minimum_available_request_fraction": {"fmp": 0.50},
    }
    partial = [{"provider": "fmp", "status": "AVAILABLE"} for _ in range(5)]
    partial.extend({"provider": "fmp", "status": "EMPTY"} for _ in range(5))
    status, diagnostics = _provider_acceptance(partial, ["fmp"], policy)
    assert status == "PASS"
    assert diagnostics["hard_failures"] == []

    outage = [{"provider": "fmp", "status": "AVAILABLE"}]
    outage.extend({"provider": "fmp", "status": "REQUEST_ERROR"} for _ in range(9))
    status, diagnostics = _provider_acceptance(outage, ["fmp"], policy)
    assert status == "FAIL"
    assert "fmp:clean_fraction" in diagnostics["hard_failures"]
    assert "fmp:available_fraction" in diagnostics["hard_failures"]


def test_normalized_rows_isolate_bad_bands_and_conflicting_duplicates() -> None:
    base = {
        "provider": "fmp",
        "endpoint_id": "analyst_estimates",
        "ticker": "AAA",
        "fiscal_period_end": "2026-12-31",
        "fiscal_period": "annual",
        "estimate_type": "eps_annual",
        "estimate_average": 2.0,
        "estimate_low": 1.0,
        "estimate_high": 3.0,
        "analyst_count": 10,
    }
    assert _validated_normalized_rows([base]) == [base]
    with pytest.raises(ValueError, match="Non-finite"):
        _validated_normalized_rows([{**base, "estimate_average": float("nan")}])
    with pytest.raises(ValueError, match="Invalid normalized count"):
        _validated_normalized_rows([{**base, "analyst_count": 1.5}])
    inverted = {**base, "estimate_low": 4.0}
    assert _validated_normalized_rows([inverted]) == [inverted]
    assert _validated_normalized_rows([base, dict(base)]) == [base]
    with pytest.raises(ValueError, match="Conflicting normalized"):
        _validated_normalized_rows([base, {**base, "estimate_average": 2.1}])


def test_fmp_alternate_estimate_fields_are_normalized() -> None:
    result = ProviderPayloadResult(
        provider="fmp",
        capability="analyst_estimates",
        symbol="AAA",
        requested_at_utc="2026-08-13T12:00:00+00:00",
        response_received_at_utc="2026-08-13T12:00:01+00:00",
        status="AVAILABLE",
        http_status=200,
        elapsed_ms=10,
        payload_kind="list",
        row_count=1,
        field_names="date,estimatedEpsAvg,estimatedRevenueAvg",
        detail="ok",
        response_sha256="a" * 64,
        payload=[
            {
                "date": "2026-12-31",
                "estimatedEpsAvg": "2.5",
                "estimatedEpsHigh": "3.0",
                "estimatedEpsLow": "2.0",
                "numberAnalystEstimatedEps": "8",
                "estimatedRevenueAvg": "1000",
                "estimatedRevenueHigh": "1100",
                "estimatedRevenueLow": "900",
                "numberAnalystEstimatedRevenue": "7",
            }
        ],
    )
    rows = normalize_estimates(
        result,
        snapshot_run_id="test",
        retrieval_cycle="test",
        entitlement_version="test",
    )
    by_type = {str(row["estimate_type"]): row for row in rows}
    assert by_type["eps_annual"]["estimate_average"] == "2.5"
    assert by_type["eps_annual"]["analyst_count"] == "8"
    assert by_type["revenue_annual"]["estimate_average"] == "1000"
    assert by_type["revenue_annual"]["analyst_count"] == "7"


def test_capture_rejects_unexpected_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPayloadResult(
        provider="fmp",
        capability="analyst_estimates",
        symbol="AAA",
        requested_at_utc="2026-08-13T12:00:00+00:00",
        response_received_at_utc="2026-08-13T12:00:01+00:00",
        status="AVAILABLE",
        http_status=200,
        elapsed_ms=10,
        payload_kind="object.data",
        row_count=1,
        field_names="date,epsAvg",
        detail="ok",
        response_sha256="a" * 64,
        payload={"data": [{"date": "2026-12-31", "epsAvg": 2.5}]},
    )
    monkeypatch.setattr(
        "portfolio_layer.provider_ingestion.capture.fetch_capability_payload",
        lambda **_: result,
    )
    record = _capture_request(
        provider="fmp",
        provider_cfg={
            "capabilities": {
                "analyst_estimates": {
                    "expected_payload": "rows",
                }
            }
        },
        endpoint="analyst_estimates",
        ticker="AAA",
        actual_date=date(2026, 8, 13),
        cycle_id="test-cycle",
        entitlement_version="test",
        timeout_sec=1.0,
        max_bytes=1_000,
        max_retries=0,
    )
    assert record["status"] == "SCHEMA_MISMATCH"
    assert record["normalized_rows"] == []


def test_payload_contract_is_case_insensitive_and_scans_all_rows() -> None:
    assert _payload_kind_matches(
        expected="object.quarterlyEarnings",
        actual="object.quarterlyEarnings",
    )
    rows: list[dict[str, object]] = [{"unrelated": index} for index in range(5)]
    rows.append({"eps_estimate_average": 2.5})
    status, kind, row_count, fields, detail = classify_payload(
        http_status=200,
        payload=rows,
        required_any_fields=["eps_estimate_average"],
    )
    assert (status, kind, row_count, detail) == ("AVAILABLE", "list", 6, "ok")
    assert "eps_estimate_average" in fields


def test_request_exception_is_contained_and_auditable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fetch(**_: object) -> object:
        raise RuntimeError("simulated provider parser failure")

    monkeypatch.setattr(
        "portfolio_layer.provider_ingestion.capture.fetch_capability_payload",
        fail_fetch,
    )
    record = _capture_request(
        provider="fmp",
        provider_cfg={"capabilities": {"analyst_estimates": {}}},
        endpoint="analyst_estimates",
        ticker="AAA",
        actual_date=date(2026, 8, 13),
        cycle_id="test-cycle",
        entitlement_version="test",
        timeout_sec=1.0,
        max_bytes=1_000,
        max_retries=0,
    )
    assert record["status"] == "REQUEST_EXCEPTION"
    assert record["normalized_rows"] == []
    assert record["detail"] == "unhandled_fetch_exception:RuntimeError"


def test_manual_capture_cannot_satisfy_scheduled_continuity(
    tmp_path: Path,
) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="premarket",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-13T11:30:00+00:00",
        )
        persist_capture(
            conn,
            cycle_id="manual-20260813-premarket",
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-13",
            actual_capture_date="2026-08-13",
            universe_id=universe_id,
            started_at_utc="2026-08-13T11:30:00+00:00",
            completed_at_utc="2026-08-13T11:31:00+00:00",
            request_records=[_request(received="2026-08-13T11:30:30+00:00", average=2.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
        )
        slot = [
            {
                "capture_date": "2026-08-13",
                "capture_phase": "premarket",
                "due_at_utc": "2026-08-13T11:30:00+00:00",
            }
        ]
        assert capture_continuity_rows(conn, slots=slot)[0]["status"] == "MISSING"

        cycle_id = "scheduled-20260813-premarket-a01"
        record_dispatch_started(
            conn,
            cycle_id=cycle_id,
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
            started_at_utc="2026-08-13T11:30:00+00:00",
            parent_pid=123,
        )
        finalize_dispatch_attempt(
            conn,
            cycle_id=cycle_id,
            completed_at_utc="2026-08-13T11:31:00+00:00",
            state="FAIL",
            return_code=7,
            artifact_path="",
            artifact_sha256="",
            detail="test failure",
        )
        row = capture_continuity_rows(conn, slots=slot)[0]
        assert row["status"] == "FAILED"
        assert row["attempt_count"] == 1
    finally:
        conn.close()


def test_stale_started_dispatch_is_interrupted_and_retry_number_is_monotone(
    tmp_path: Path,
) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        record_dispatch_started(
            conn,
            cycle_id="scheduled-20260813-premarket-a03",
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
            started_at_utc="2026-08-13T11:30:00+00:00",
            parent_pid=123,
        )
        interrupted = interrupt_stale_dispatch_attempts(
            conn,
            stale_before_utc="2026-08-13T13:00:00+00:00",
            interrupted_at_utc="2026-08-13T14:00:00+00:00",
        )
        assert interrupted == ["scheduled-20260813-premarket-a03"]
        state = conn.execute("SELECT state FROM scheduled_dispatch_attempts").fetchone()[0]
        assert state == "INTERRUPTED"
        assert verify_store(conn) == []
        with pytest.raises(ValueError, match="already terminal"):
            finalize_dispatch_attempt(
                conn,
                cycle_id="scheduled-20260813-premarket-a03",
                completed_at_utc="2026-08-13T14:01:00+00:00",
                state="FAIL",
                return_code=1,
                artifact_path="",
                artifact_sha256="",
                detail="must not rewrite terminal evidence",
            )
    finally:
        conn.close()


def test_scheduled_capture_requires_terminal_dispatch_pass(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="premarket",
            members=[{"ticker": "AAA", "tier": "tier0", "sector": "", "source_pipeline": "test"}],
            providers=["fmp"],
            created_at_utc="2026-08-13T11:30:00+00:00",
        )
        cycle_id = "scheduled-20260813-premarket-a01"
        record_dispatch_started(
            conn,
            cycle_id=cycle_id,
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
            started_at_utc="2026-08-13T11:30:00+00:00",
            parent_pid=123,
        )
        persist_capture(
            conn,
            cycle_id=cycle_id,
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-13",
            actual_capture_date="2026-08-13",
            universe_id=universe_id,
            started_at_utc="2026-08-13T11:30:00+00:00",
            completed_at_utc="2026-08-13T11:31:00+00:00",
            request_records=[_request(received="2026-08-13T11:30:30+00:00", average=2.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
        )
        finalize_dispatch_attempt(
            conn,
            cycle_id=cycle_id,
            completed_at_utc="2026-08-13T11:32:00+00:00",
            state="FAIL",
            return_code=7,
            artifact_path="",
            artifact_sha256="",
            detail="post-persist validation failed",
        )
        slot = [{"capture_date": "2026-08-13", "capture_phase": "premarket", "due_at_utc": "2026-08-13T11:30:00+00:00"}]
        row = capture_continuity_rows(conn, slots=slot)[0]
        assert row["status"] == "FAILED"
        assert row["accepted_attempt_count"] == 0
    finally:
        conn.close()
    assert (
        _next_attempt_number(
            {
                "scheduled-20260813-premarket-a01",
                "scheduled-20260813-premarket-a03",
            }
        )
        == 4
    )


def test_failed_capture_does_not_become_prior_or_emit_change(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="premarket",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-13T11:30:00+00:00",
        )
        _persist(
            conn,
            cycle="accepted-1",
            received="2026-08-03T12:00:00+00:00",
            average=2.0,
            universe_id=universe_id,
        )
        persist_capture(
            conn,
            cycle_id="failed-middle",
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-04",
            actual_capture_date="2026-08-04",
            universe_id=universe_id,
            started_at_utc="2026-08-04T12:00:00+00:00",
            completed_at_utc="2026-08-04T12:01:00+00:00",
            request_records=[_request(received="2026-08-04T12:00:00+00:00", average=9.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="FAIL",
        )
        _persist(
            conn,
            cycle="accepted-2",
            received="2026-08-05T12:00:00+00:00",
            average=2.2,
            universe_id=universe_id,
        )
        change = conn.execute("SELECT estimate_average_before,estimate_average_after FROM estimate_changes").fetchone()
        assert tuple(change) == (2.0, 2.2)
        assert conn.execute("SELECT COUNT(*) FROM estimate_changes").fetchone()[0] == 1
        assert verify_store(conn) == []
    finally:
        conn.close()


def test_missing_capture_manifest_is_repaired_from_db_seal(tmp_path: Path) -> None:
    store_path = tmp_path / "provider.sqlite"
    cycle_id = "scheduled-20260813-premarket-a01"
    cycle_dir = tmp_path / "artifacts" / cycle_id
    cycle_dir.mkdir(parents=True)
    report = cycle_dir / "capture_requests.csv"
    report.write_text("ticker,status\nAAA,AVAILABLE\n", encoding="utf-8")
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()

    conn = connect_store(store_path)
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="premarket",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-13T11:30:00+00:00",
        )
        persist_capture(
            conn,
            cycle_id=cycle_id,
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-13",
            actual_capture_date="2026-08-13",
            universe_id=universe_id,
            started_at_utc="2026-08-13T11:30:00+00:00",
            completed_at_utc="2026-08-13T11:31:00+00:00",
            request_records=[_request(received="2026-08-13T11:30:30+00:00", average=2.0)],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
            metadata={
                "raw_payloads_retained": False,
                "universe_as_of": "2026-08-13",
                "artifact_contract": {
                    "report_name": report.name,
                    "report_sha256": report_sha,
                    "inputs_sha256": {"config": "c" * 64},
                },
            },
        )
        run_row = conn.execute("SELECT * FROM capture_runs WHERE cycle_id=?", (cycle_id,)).fetchone()
        assert run_row is not None
        manifest_path, errors = ensure_capture_manifest(
            conn,
            row=dict(run_row),
            cycle_dir=cycle_dir,
            store_path=store_path,
        )
        assert errors == []
        assert manifest_path is not None and manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["outputs_sha256"] == {report.name: report_sha}
        assert manifest["raw_payloads_retained"] is False
        manifest["acceptance"] = "FAIL"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        repaired_path, errors = ensure_capture_manifest(
            conn,
            row=dict(run_row),
            cycle_dir=cycle_dir,
            store_path=store_path,
        )
        assert repaired_path is None
        assert errors == [f"artifact_existing_manifest_mismatch:{cycle_id}"]
    finally:
        conn.close()


def test_response_digest_and_scheduler_paths_are_fail_closed(tmp_path: Path) -> None:
    assert _valid_response_sha256("a" * 64)
    assert not _valid_response_sha256("")
    assert not _valid_response_sha256("g" * 64)
    assert _validated_cycle_id("scheduled-20260813-postclose-a01") == "scheduled-20260813-postclose-a01"
    with pytest.raises(ValueError, match="cycle ID"):
        _validated_cycle_id("../outside")
    with pytest.raises(ValueError, match="single path component|escapes output root"):
        _write_scheduler_attempt(
            output_root=tmp_path,
            cycle_id="../outside",
            command=["python", "capture.py"],
            invoked_at_utc=datetime(2026, 8, 13, 12, tzinfo=timezone.utc),
        )
    assert not (tmp_path.parent / "outside").exists()


def test_store_rejects_clean_response_without_digest_atomically(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="premarket",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-13T11:30:00+00:00",
        )
        invalid = _request(
            received="2026-08-13T11:30:30+00:00",
            average=2.0,
        )
        invalid["response_sha256"] = ""
        with pytest.raises(ValueError, match="valid SHA-256"):
            persist_capture(
                conn,
                cycle_id="invalid-clean-response",
                capture_phase="premarket",
                requested_portfolio_as_of="2026-08-13",
                actual_capture_date="2026-08-13",
                universe_id=universe_id,
                started_at_utc="2026-08-13T11:30:00+00:00",
                completed_at_utc="2026-08-13T11:31:00+00:00",
                request_records=[invalid],
                source_code_digest="b" * 64,
                config_digest="c" * 64,
                timezone_name="America/New_York",
                calendar_name="XNYS",
                decision_cutoff_local="09:25",
                status="PASS",
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM capture_runs WHERE cycle_id='invalid-clean-response'").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_summary_log_is_rotated_under_a_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portfolio_layer.provider_ingestion.run_due.SUMMARY_LOG_MAX_BYTES",
        80,
    )
    monkeypatch.setattr(
        "portfolio_layer.provider_ingestion.run_due.SUMMARY_LOG_BACKUP_COUNT",
        2,
    )
    write_summary_log(
        output_root=tmp_path,
        lines=["first " + "x" * 60],
        verified=True,
        invoked_at_utc=datetime(2026, 8, 13, 12, tzinfo=timezone.utc),
    )
    write_summary_log(
        output_root=tmp_path,
        lines=["second " + "y" * 60],
        verified=False,
        invoked_at_utc=datetime(2026, 8, 13, 13, tzinfo=timezone.utc),
    )
    assert (tmp_path / "run_due_summary.log.1").is_file()
    latest = (tmp_path / "run_due_summary_latest.txt").read_text(encoding="utf-8")
    assert "second" in latest
    assert "verification=FAIL" in latest


def test_provider_ingestion_policy_is_fully_bound_to_runtime() -> None:
    config_path = Path(__file__).resolve().parents[2] / "portfolio_layer" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    ingestion = config["provider_ingestion"]
    validate_provider_ingestion_policy(ingestion)

    disabled = {**ingestion, "enabled": False}
    with pytest.raises(ValueError, match="enabled must be true"):
        validate_provider_ingestion_policy(disabled)

    drifted_actionability = {
        **ingestion,
        "actionability": {
            **ingestion["actionability"],
            "post_cutoff_observations_effective_next_session": False,
        },
    }
    with pytest.raises(ValueError, match="actionability contract"):
        validate_provider_ingestion_policy(drifted_actionability)

    invalid_schedule = {
        **ingestion,
        "schedules": {**ingestion["schedules"], "priority_refresh": "10:00"},
    }
    with pytest.raises(ValueError, match="decision cutoff"):
        validate_provider_ingestion_policy(invalid_schedule)


def test_store_rejects_duplicate_normalized_keys_atomically(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    universe_id = freeze_universe(
        conn,
        source_run_as_of="2026-08-03",
        capture_phase="premarket",
        members=[{"ticker": "AAA", "tier": "tier0", "sector": "Test", "source_pipeline": "test"}],
        providers=["fmp"],
        created_at_utc="2026-08-03T11:59:00+00:00",
    )
    request = _request(received="2026-08-03T12:00:00+00:00", average=2.0)
    rows = request["normalized_rows"]
    assert isinstance(rows, list)
    rows.append(dict(rows[0]))

    with pytest.raises(ValueError, match="duplicate rows"):
        persist_capture(
            conn,
            cycle_id="duplicate-normalized-key",
            capture_phase="premarket",
            requested_portfolio_as_of="2026-08-03",
            actual_capture_date="2026-08-03",
            universe_id=universe_id,
            started_at_utc="2026-08-03T11:59:00+00:00",
            completed_at_utc="2026-08-03T12:01:00+00:00",
            request_records=[request],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
        )
    assert conn.execute("SELECT COUNT(*) FROM capture_runs").fetchone()[0] == 0
    assert verify_store(conn) == []
    conn.close()


def test_readonly_store_connection_cannot_mutate_schema_or_data(tmp_path: Path) -> None:
    store_path = tmp_path / "provider.sqlite"
    conn = connect_store(store_path)
    conn.close()

    readonly = connect_store_readonly(store_path)
    assert readonly.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        readonly.execute("INSERT INTO instruments(instrument_id,canonical_ticker) VALUES('x','AAA')")
    readonly.close()


def test_missing_or_corrupt_capture_report_is_repaired_from_db_seal(tmp_path: Path) -> None:
    store_path = tmp_path / "provider.sqlite"
    cycle_id = "scheduled-20260813-postclose-a01"
    cycle_dir = tmp_path / "artifacts" / cycle_id
    cycle_dir.mkdir(parents=True)
    report = cycle_dir / "capture_requests.csv"
    request = _request(received="2026-08-13T22:01:00+00:00", average=2.0)
    write_csv(report, REPORT_FIELDS, capture_report_rows([request]))
    expected_bytes = report.read_bytes()
    report_sha = hashlib.sha256(expected_bytes).hexdigest()

    conn = connect_store(store_path)
    try:
        universe_id = freeze_universe(
            conn,
            source_run_as_of="2026-08-13",
            capture_phase="postclose",
            members=[
                {
                    "ticker": "AAA",
                    "tier": "tier0",
                    "sector": "",
                    "source_pipeline": "test",
                }
            ],
            providers=["fmp"],
            created_at_utc="2026-08-13T22:00:00+00:00",
        )
        persist_capture(
            conn,
            cycle_id=cycle_id,
            capture_phase="postclose",
            requested_portfolio_as_of="2026-08-13",
            actual_capture_date="2026-08-13",
            universe_id=universe_id,
            started_at_utc="2026-08-13T22:00:00+00:00",
            completed_at_utc="2026-08-13T22:02:00+00:00",
            request_records=[request],
            source_code_digest="b" * 64,
            config_digest="c" * 64,
            timezone_name="America/New_York",
            calendar_name="XNYS",
            decision_cutoff_local="09:25",
            status="PASS",
            metadata={
                "raw_payloads_retained": False,
                "universe_as_of": "2026-08-13",
                "artifact_contract": {
                    "report_name": report.name,
                    "report_sha256": report_sha,
                    "report_order_schema": REPORT_ORDER_SCHEMA,
                    "report_order": capture_report_order([request]),
                    "inputs_sha256": {"config": "c" * 64},
                },
            },
        )
        run_row = conn.execute("SELECT * FROM capture_runs WHERE cycle_id=?", (cycle_id,)).fetchone()
        assert run_row is not None
        manifest_path, errors = ensure_capture_manifest(
            conn,
            row=dict(run_row),
            cycle_dir=cycle_dir,
            store_path=store_path,
        )
        assert errors == []
        assert manifest_path is not None and manifest_path.is_file()

        report.unlink()
        repaired_path, errors = ensure_capture_manifest(
            conn,
            row=dict(run_row),
            cycle_dir=cycle_dir,
            store_path=store_path,
        )
        assert errors == []
        assert repaired_path == manifest_path
        assert report.read_bytes() == expected_bytes

        report.write_text("corrupt\n", encoding="utf-8")
        repaired_path, errors = ensure_capture_manifest(
            conn,
            row=dict(run_row),
            cycle_dir=cycle_dir,
            store_path=store_path,
        )
        assert errors == []
        assert repaired_path == manifest_path
        assert report.read_bytes() == expected_bytes
    finally:
        conn.close()


def test_scheduled_capture_requires_matching_active_dispatch(tmp_path: Path) -> None:
    conn = connect_store(tmp_path / "provider.sqlite")
    cycle_id = "scheduled-20260813-premarket-a01"
    try:
        require_scheduled_dispatch(
            conn,
            cycle_id="manual-20260813-premarket",
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
        )
        with pytest.raises(ValueError, match="lacks a durable dispatch"):
            require_scheduled_dispatch(
                conn,
                cycle_id=cycle_id,
                actual_capture_date="2026-08-13",
                capture_phase="premarket",
            )

        record_dispatch_started(
            conn,
            cycle_id=cycle_id,
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
            started_at_utc="2026-08-13T11:30:00+00:00",
            parent_pid=123,
        )
        require_scheduled_dispatch(
            conn,
            cycle_id=cycle_id,
            actual_capture_date="2026-08-13",
            capture_phase="premarket",
        )
        with pytest.raises(ValueError, match="date differs"):
            require_scheduled_dispatch(
                conn,
                cycle_id=cycle_id,
                actual_capture_date="2026-08-14",
                capture_phase="premarket",
            )
        with pytest.raises(ValueError, match="phase differs"):
            require_scheduled_dispatch(
                conn,
                cycle_id=cycle_id,
                actual_capture_date="2026-08-13",
                capture_phase="postclose",
            )

        finalize_dispatch_attempt(
            conn,
            cycle_id=cycle_id,
            completed_at_utc="2026-08-13T11:31:00+00:00",
            state="FAIL",
            return_code=1,
            artifact_path="",
            artifact_sha256="",
            detail="test",
        )
        with pytest.raises(ValueError, match="not active"):
            require_scheduled_dispatch(
                conn,
                cycle_id=cycle_id,
                actual_capture_date="2026-08-13",
                capture_phase="premarket",
            )
    finally:
        conn.close()


def test_simulated_clocks_cannot_mutate_recovery_or_live_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path(__file__).resolve().parents[2] / "portfolio_layer" / "config.yaml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover.py",
            "--config",
            str(config_path),
            "--now-utc",
            "2026-08-13T12:00:00+00:00",
            "--execute",
        ],
    )
    with pytest.raises(ValueError, match="diagnostic-only.*--execute"):
        recover_main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate.py",
            "--config",
            str(config_path),
            "--now-utc",
            "2026-08-13T12:00:00+00:00",
        ],
    )
    with pytest.raises(ValueError, match="requires --output-dir"):
        validate_main()


def test_force_empty_publication_retires_prior_active_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _setup_run_due_env(tmp_path)
    publication_dir = tmp_path / "publication"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish.py",
            "--config",
            str(config_path),
            "--as-of",
            "2026-08-03",
            "--output-dir",
            str(publication_dir),
        ],
    )
    assert publish_main() == 0
    store_path = tmp_path / "db" / "provider_observations.sqlite"
    conn = connect_store_readonly(store_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM artifact_dependencies WHERE status='active'").fetchone()[0] > 0
    finally:
        conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish.py",
            "--config",
            str(config_path),
            "--as-of",
            "2026-08-02",
            "--output-dir",
            str(publication_dir),
            "--force",
        ],
    )
    assert publish_main() == 0
    conn = connect_store_readonly(store_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM artifact_dependencies WHERE status='active'").fetchone()[0] == 0
    finally:
        conn.close()


def test_store_verifier_detects_derived_data_tampering(tmp_path: Path) -> None:
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
            created_at_utc="2026-08-03T11:30:00+00:00",
        )
        _persist(
            conn,
            cycle="integrity-cycle-1",
            received="2026-08-03T12:00:00+00:00",
            average=2.0,
            universe_id=universe_id,
        )
        _persist(
            conn,
            cycle="integrity-cycle-2",
            received="2026-08-03T12:30:00+00:00",
            average=2.2,
            universe_id=universe_id,
        )
        assert verify_store(conn) == []

        conn.execute("UPDATE estimate_versions SET estimate_average=99 WHERE estimate_average=2.2")
        assert any(item.startswith("version_content_digest_mismatch:") for item in verify_store(conn))
        conn.rollback()

        conn.execute("UPDATE estimate_versions SET provider_symbol='ZZZ'")
        assert any(item.startswith("observation_request_version_mismatch:") for item in verify_store(conn))
        conn.rollback()

        conn.execute("UPDATE estimate_changes SET estimate_average_delta=99")
        assert any(item.startswith("change_delta_mismatch:") for item in verify_store(conn))
        conn.rollback()

        conn.execute("UPDATE coverage_daily SET status='EMPTY'")
        assert any(item.startswith("coverage_row_mismatch:") for item in verify_store(conn))
        conn.rollback()

        conn.execute(
            "UPDATE capture_universe_members SET sector='Tampered' WHERE universe_id=?",
            (universe_id,),
        )
        errors = verify_store(conn)
        assert any(item.startswith("universe_digest_mismatch:") for item in errors)
        assert any(item.startswith("universe_id_mismatch:") for item in errors)
        conn.rollback()
        assert verify_store(conn) == []
    finally:
        conn.close()
