"""SQLite contracts and PIT actionability rules for provider observations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Mapping, Sequence, cast
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "provider_observation_store_v2"
EMPTY_DIGEST = "0" * 64
CLEAN_REQUEST_STATUSES = frozenset({"AVAILABLE", "EMPTY"})
VERSION_VALUE_FIELDS = (
    "estimate_average",
    "estimate_high",
    "estimate_low",
    "analyst_count",
    "estimate_average_7_days_ago",
    "estimate_average_30_days_ago",
    "estimate_average_60_days_ago",
    "estimate_average_90_days_ago",
    "revision_up_7_days",
    "revision_down_7_days",
    "revision_up_30_days",
    "revision_down_30_days",
)


DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    schema_version TEXT PRIMARY KEY,
    applied_at_utc TEXT NOT NULL,
    source_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    canonical_ticker TEXT NOT NULL,
    exchange_calendar TEXT NOT NULL DEFAULT 'XNYS',
    valid_from TEXT NOT NULL DEFAULT '1900-01-01',
    valid_to TEXT,
    UNIQUE(canonical_ticker, valid_from)
);
CREATE TABLE IF NOT EXISTS provider_symbols (
    instrument_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '1900-01-01',
    valid_to TEXT,
    PRIMARY KEY(instrument_id, provider, valid_from),
    FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE IF NOT EXISTS capture_universes (
    universe_id TEXT PRIMARY KEY,
    source_run_as_of TEXT NOT NULL,
    capture_phase TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    universe_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_universe_members (
    universe_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    tier TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT '',
    source_pipeline TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(universe_id, instrument_id),
    FOREIGN KEY(universe_id) REFERENCES capture_universes(universe_id),
    FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE IF NOT EXISTS provider_universe_registry (
    registry_id TEXT PRIMARY KEY,
    source_run_as_of TEXT NOT NULL,
    activated_at_utc TEXT NOT NULL,
    source_artifact_path TEXT NOT NULL,
    source_artifact_sha256 TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    universe_digest TEXT NOT NULL,
    UNIQUE(source_run_as_of, universe_digest)
);
CREATE TABLE IF NOT EXISTS provider_universe_registry_members (
    registry_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('tier0','tier1','tier2')),
    sector TEXT NOT NULL DEFAULT '',
    source_pipeline TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(registry_id, instrument_id),
    FOREIGN KEY(registry_id) REFERENCES provider_universe_registry(registry_id),
    FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS ix_provider_universe_registry_asof
ON provider_universe_registry(source_run_as_of, activated_at_utc);
CREATE TABLE IF NOT EXISTS capture_runs (
    run_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL UNIQUE,
    capture_phase TEXT NOT NULL,
    requested_portfolio_as_of TEXT NOT NULL DEFAULT '',
    actual_capture_date TEXT NOT NULL,
    universe_id TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    decision_cutoff_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PASS','PASS_WITH_WARNINGS','FAIL','MIGRATED')),
    request_count INTEGER NOT NULL,
    available_request_count INTEGER NOT NULL,
    empty_request_count INTEGER NOT NULL,
    error_request_count INTEGER NOT NULL,
    normalized_row_count INTEGER NOT NULL,
    new_version_count INTEGER NOT NULL,
    unchanged_observation_count INTEGER NOT NULL,
    previous_pass_digest TEXT NOT NULL,
    run_digest TEXT NOT NULL,
    source_code_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(universe_id) REFERENCES capture_universes(universe_id)
);
CREATE TABLE IF NOT EXISTS scheduled_dispatch_attempts (
    cycle_id TEXT PRIMARY KEY,
    actual_capture_date TEXT NOT NULL,
    capture_phase TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('STARTED','PASS','FAIL','INTERRUPTED')),
    return_code INTEGER,
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    parent_pid INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempt_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scheduled_dispatch_slot
ON scheduled_dispatch_attempts(actual_capture_date, capture_phase, started_at_utc);
CREATE TABLE IF NOT EXISTS capture_requests (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    request_started_at_utc TEXT NOT NULL,
    response_received_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    elapsed_ms INTEGER NOT NULL,
    provider_row_count INTEGER NOT NULL,
    normalized_row_count INTEGER NOT NULL,
    response_sha256 TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, provider, endpoint_id, provider_symbol),
    FOREIGN KEY(run_id) REFERENCES capture_runs(run_id),
    FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE TABLE IF NOT EXISTS estimate_versions (
    version_id TEXT PRIMARY KEY,
    natural_key_hash TEXT NOT NULL,
    normalized_content_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    estimate_type TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT '',
    units TEXT NOT NULL DEFAULT '',
    split_basis TEXT NOT NULL DEFAULT 'provider_defined_unknown',
    estimate_definition TEXT NOT NULL DEFAULT 'provider_consensus_unknown_gaap_adjusted',
    semantic_basis_hash TEXT NOT NULL,
    estimate_average REAL,
    estimate_high REAL,
    estimate_low REAL,
    analyst_count INTEGER,
    estimate_average_7_days_ago REAL,
    estimate_average_30_days_ago REAL,
    estimate_average_60_days_ago REAL,
    estimate_average_90_days_ago REAL,
    revision_up_7_days INTEGER,
    revision_down_7_days INTEGER,
    revision_up_30_days INTEGER,
    revision_down_30_days INTEGER,
    created_at_utc TEXT NOT NULL,
    UNIQUE(natural_key_hash, normalized_content_sha256),
    FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS ix_estimate_versions_natural
ON estimate_versions(natural_key_hash, created_at_utc);
CREATE TABLE IF NOT EXISTS estimate_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    available_at_utc TEXT NOT NULL,
    effective_trading_date TEXT NOT NULL,
    effective_from_utc TEXT NOT NULL,
    same_session_eligible INTEGER NOT NULL CHECK(same_session_eligible IN (0,1)),
    unchanged_from_prior INTEGER NOT NULL CHECK(unchanged_from_prior IN (0,1)),
    prior_observation_id TEXT,
    observation_digest TEXT NOT NULL,
    UNIQUE(run_id, request_id, version_id),
    FOREIGN KEY(run_id) REFERENCES capture_runs(run_id),
    FOREIGN KEY(request_id) REFERENCES capture_requests(request_id),
    FOREIGN KEY(version_id) REFERENCES estimate_versions(version_id)
);
CREATE INDEX IF NOT EXISTS ix_estimate_observations_pit
ON estimate_observations(available_at_utc, effective_trading_date, version_id);
CREATE INDEX IF NOT EXISTS ix_estimate_observations_version
ON estimate_observations(version_id, available_at_utc, observation_id);
CREATE TABLE IF NOT EXISTS estimate_changes (
    change_id TEXT PRIMARY KEY,
    natural_key_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    estimate_type TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,
    prior_observation_id TEXT NOT NULL,
    new_observation_id TEXT NOT NULL,
    prior_version_id TEXT NOT NULL,
    new_version_id TEXT NOT NULL,
    interval_start_utc TEXT NOT NULL,
    interval_end_utc TEXT NOT NULL,
    estimate_average_before REAL,
    estimate_average_after REAL,
    estimate_average_delta REAL,
    changed_fields_json TEXT NOT NULL,
    FOREIGN KEY(prior_observation_id) REFERENCES estimate_observations(observation_id),
    FOREIGN KEY(new_observation_id) REFERENCES estimate_observations(observation_id)
);
CREATE TABLE IF NOT EXISTS coverage_daily (
    coverage_date TEXT NOT NULL,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    available_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    PRIMARY KEY(coverage_date, run_id, provider, instrument_id),
    FOREIGN KEY(run_id) REFERENCES capture_runs(run_id)
);
CREATE TABLE IF NOT EXISTS artifact_dependencies (
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','invalidated')),
    PRIMARY KEY(artifact_path, artifact_sha256, observation_id),
    FOREIGN KEY(observation_id) REFERENCES estimate_observations(observation_id)
);
CREATE TABLE IF NOT EXISTS legacy_migration_annotations (
    run_id TEXT PRIMARY KEY,
    legacy_retrieval_cycle TEXT NOT NULL,
    stated_as_of_date TEXT NOT NULL,
    observed_capture_date TEXT NOT NULL,
    legacy_asof_mismatch INTEGER NOT NULL CHECK(legacy_asof_mismatch IN (0,1)),
    annotation_version TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    annotation_digest TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES capture_runs(run_id)
);
DROP VIEW IF EXISTS provider_estimate_snapshots;
CREATE VIEW provider_estimate_snapshots AS
SELECT o.observation_id AS snapshot_id, o.run_id AS snapshot_run_id,
       v.provider, v.endpoint_id, v.ticker, v.fiscal_period_end,
       v.fiscal_period, v.estimate_type, v.estimate_average,
       v.estimate_high, v.estimate_low, v.analyst_count,
       v.estimate_average_7_days_ago, v.estimate_average_30_days_ago,
       v.estimate_average_60_days_ago, v.estimate_average_90_days_ago,
       v.revision_up_7_days, v.revision_down_7_days,
       v.revision_up_30_days, v.revision_down_30_days, v.currency,
       '' AS provider_published_at_utc,
       r.request_started_at_utc AS fetched_at_utc, o.available_at_utc,
       cr.cycle_id AS retrieval_cycle, v.natural_key_hash AS source_uid,
       r.response_sha256, v.normalized_content_sha256 AS normalized_sha256,
       'provider_entitlements_v1:provisional_retention_v1' AS entitlement_version,
       'provisional_user_authorized' AS retention_class,
       'available' AS coverage_status, o.effective_trading_date,
       o.effective_from_utc, o.same_session_eligible, v.instrument_id,
       v.provider_symbol, v.semantic_basis_hash
FROM estimate_observations o
JOIN estimate_versions v ON v.version_id=o.version_id
JOIN capture_requests r ON r.request_id=o.request_id
JOIN capture_runs cr ON cr.run_id=o.run_id
WHERE cr.status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED');
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def connect_store(path: Path, *, timeout_sec: float = 30.0) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
    conn.executescript(DDL)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations VALUES(?,?,?)",
            (SCHEMA_VERSION, utc_now(), source_digest(Path(__file__).resolve())),
        )
    return conn


def connect_store_readonly(path: Path, *, timeout_sec: float = 30.0) -> sqlite3.Connection:
    """Open an existing provider store without issuing DDL or acquiring a write lock."""
    if not path.is_file():
        raise FileNotFoundError(f"Provider observation store is missing: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE schema_version=?",
            (SCHEMA_VERSION,),
        ).fetchone()
    except sqlite3.Error:
        conn.close()
        raise
    if row is None:
        conn.close()
        raise RuntimeError(f"Provider observation store schema {SCHEMA_VERSION} is not initialized")
    return conn


def _write_lock_metadata(fd: int, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, encoded)
    os.ftruncate(fd, len(encoded))
    os.fsync(fd)


def _try_lock_file(fd: int) -> bool:
    """Acquire one process-owned byte lock without blocking.

    Windows releases this lock automatically when a process exits, including
    forced termination. The on-disk JSON is diagnostic metadata, not the lock.
    """
    if os.name != "nt":
        raise RuntimeError("Provider writer locking currently requires Windows")
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock_file(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def writer_lock(path: Path, *, timeout_sec: float = 30.0) -> Iterator[None]:
    """Serialize provider-store writers with a crash-safe Windows file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    token = uuid.uuid4().hex
    pid = os.getpid()
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        while not _try_lock_file(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for provider-store writer lock: {path}")
            time.sleep(0.1)
        acquired_at = utc_now()
        _write_lock_metadata(
            fd,
            {
                "token": token,
                "pid": pid,
                "host": socket.gethostname(),
                "state": "active",
                "acquired_at_utc": acquired_at,
            },
        )
        try:
            yield
        finally:
            _write_lock_metadata(
                fd,
                {
                    "token": token,
                    "pid": pid,
                    "host": socket.gethostname(),
                    "state": "released",
                    "acquired_at_utc": acquired_at,
                    "released_at_utc": utc_now(),
                },
            )
            _unlock_file(fd)
    finally:
        os.close(fd)


def instrument_id(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized or normalized == "CASH" or any(char.isspace() for char in normalized):
        raise ValueError(f"Invalid provider instrument ticker: {ticker!r}")
    return f"US_EQ:{normalized}"


def ensure_instrument(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    providers: Iterable[str],
) -> str:
    ident = instrument_id(ticker)
    conn.execute(
        "INSERT OR IGNORE INTO instruments(instrument_id,canonical_ticker) VALUES(?,?)",
        (ident, ticker.strip().upper()),
    )
    for provider in providers:
        conn.execute(
            "INSERT OR IGNORE INTO provider_symbols(instrument_id,provider,provider_symbol) VALUES(?,?,?)",
            (ident, provider, ticker.strip().upper()),
        )
    return ident


def _dispatch_attempt_identity(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": str(values["cycle_id"]),
        "actual_capture_date": str(values["actual_capture_date"]),
        "capture_phase": str(values["capture_phase"]),
        "started_at_utc": str(values["started_at_utc"]),
        "completed_at_utc": str(values.get("completed_at_utc", "")),
        "state": str(values["state"]),
        "return_code": values.get("return_code"),
        "artifact_path": str(values.get("artifact_path", "")),
        "artifact_sha256": str(values.get("artifact_sha256", "")),
        "parent_pid": int(values["parent_pid"]),
        "detail": str(values.get("detail", "")),
    }


def require_scheduled_dispatch(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    actual_capture_date: str,
    capture_phase: str,
) -> None:
    """Require scheduler-owned provenance before a scheduled capture starts."""
    if not cycle_id.startswith("scheduled-"):
        return
    row = conn.execute(
        "SELECT actual_capture_date,capture_phase,state FROM scheduled_dispatch_attempts WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Scheduled capture lacks a durable dispatch record: {cycle_id}")
    if str(row["actual_capture_date"]) != actual_capture_date:
        raise ValueError(f"Scheduled capture date differs from its dispatch record: {cycle_id}")
    if str(row["capture_phase"]) != capture_phase:
        raise ValueError(f"Scheduled capture phase differs from its dispatch record: {cycle_id}")
    if str(row["state"]) != "STARTED":
        raise ValueError(f"Scheduled capture dispatch is not active: {cycle_id}")


def record_dispatch_started(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    actual_capture_date: str,
    capture_phase: str,
    started_at_utc: str,
    parent_pid: int,
) -> None:
    """Durably record a scheduler launch before the child process starts."""
    date.fromisoformat(actual_capture_date)
    _parse_utc(started_at_utc)
    if not cycle_id.strip() or not capture_phase.strip():
        raise ValueError("Scheduled dispatch identity fields must be non-empty")
    if parent_pid <= 0:
        raise ValueError("Scheduled dispatch parent_pid must be positive")
    if conn.execute("SELECT 1 FROM scheduled_dispatch_attempts WHERE cycle_id=?", (cycle_id,)).fetchone():
        raise ValueError(f"Scheduled dispatch attempt already exists: {cycle_id}")
    values: dict[str, Any] = {
        "cycle_id": cycle_id,
        "actual_capture_date": actual_capture_date,
        "capture_phase": capture_phase,
        "started_at_utc": started_at_utc,
        "completed_at_utc": "",
        "state": "STARTED",
        "return_code": None,
        "artifact_path": "",
        "artifact_sha256": "",
        "parent_pid": parent_pid,
        "detail": "child launch recorded before execution",
    }
    values["attempt_digest"] = digest(_dispatch_attempt_identity(values))
    columns = tuple(values)
    with conn:
        conn.execute(
            f"INSERT INTO scheduled_dispatch_attempts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )


def finalize_dispatch_attempt(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    completed_at_utc: str,
    state: Literal["PASS", "FAIL", "INTERRUPTED"],
    return_code: int | None,
    artifact_path: str,
    artifact_sha256: str,
    detail: str,
) -> None:
    """Seal the terminal state of a previously started scheduler attempt."""
    _parse_utc(completed_at_utc)
    row = conn.execute("SELECT * FROM scheduled_dispatch_attempts WHERE cycle_id=?", (cycle_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown scheduled dispatch attempt: {cycle_id}")
    values = dict(row)
    if str(row["state"]) != "STARTED":
        raise ValueError(f"Scheduled dispatch attempt is already terminal: {cycle_id}")
    completed = _parse_utc(completed_at_utc)
    if completed < _parse_utc(str(row["started_at_utc"])):
        raise ValueError("Scheduled dispatch completion precedes its start")
    if state == "PASS" and return_code != 0:
        raise ValueError("PASS dispatch requires return_code=0")
    if state == "FAIL" and (return_code is None or return_code == 0):
        raise ValueError("FAIL dispatch requires a nonzero return code")
    if state == "INTERRUPTED" and return_code is not None:
        raise ValueError("INTERRUPTED dispatch must not invent a return code")
    if bool(artifact_path) != bool(artifact_sha256):
        raise ValueError("Dispatch artifact path and hash must be present together")
    if artifact_sha256 and not _is_sha256(artifact_sha256):
        raise ValueError("Dispatch artifact hash is not SHA-256")
    values.update(
        {
            "completed_at_utc": completed_at_utc,
            "state": state,
            "return_code": return_code,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "detail": detail,
        }
    )
    attempt_digest = digest(_dispatch_attempt_identity(values))
    with conn:
        conn.execute(
            "UPDATE scheduled_dispatch_attempts SET completed_at_utc=?,state=?,"
            "return_code=?,artifact_path=?,artifact_sha256=?,detail=?,attempt_digest=? "
            "WHERE cycle_id=?",
            (
                completed_at_utc,
                state,
                return_code,
                artifact_path,
                artifact_sha256,
                detail,
                attempt_digest,
                cycle_id,
            ),
        )


def interrupt_stale_dispatch_attempts(
    conn: sqlite3.Connection,
    *,
    stale_before_utc: str,
    interrupted_at_utc: str,
) -> list[str]:
    """Seal abandoned STARTED dispatches so a later invocation can retry.

    The scheduler child has its own timeout. A STARTED row older than the
    separately configured stale threshold therefore represents a parent crash,
    machine shutdown, or forced task termination rather than an active capture.
    """
    cutoff = _parse_utc(stale_before_utc)
    interrupted = _parse_utc(interrupted_at_utc)
    if interrupted < cutoff:
        raise ValueError("interrupted_at_utc must not precede stale_before_utc")
    rows = conn.execute(
        "SELECT * FROM scheduled_dispatch_attempts "
        "WHERE state='STARTED' AND started_at_utc<? "
        "ORDER BY started_at_utc,cycle_id",
        (cutoff.replace(microsecond=0).isoformat(),),
    ).fetchall()
    interrupted_ids: list[str] = []
    with conn:
        for row in rows:
            values = dict(row)
            values.update(
                {
                    "completed_at_utc": interrupted.replace(microsecond=0).isoformat(),
                    "state": "INTERRUPTED",
                    "return_code": None,
                    "detail": "scheduler attempt exceeded stale-start threshold",
                }
            )
            attempt_digest = digest(_dispatch_attempt_identity(values))
            conn.execute(
                "UPDATE scheduled_dispatch_attempts SET completed_at_utc=?,"
                "state='INTERRUPTED',return_code=NULL,detail=?,attempt_digest=? "
                "WHERE cycle_id=?",
                (
                    values["completed_at_utc"],
                    values["detail"],
                    attempt_digest,
                    values["cycle_id"],
                ),
            )
            interrupted_ids.append(str(values["cycle_id"]))
    return interrupted_ids


def freeze_universe(
    conn: sqlite3.Connection,
    *,
    source_run_as_of: str,
    capture_phase: str,
    members: Sequence[Mapping[str, Any]],
    providers: Sequence[str],
    created_at_utc: str,
) -> str:
    normalized = [
        {
            "ticker": str(row["ticker"]).strip().upper(),
            "tier": str(row.get("tier", "")),
            "sector": str(row.get("sector", "")),
            "source_pipeline": str(row.get("source_pipeline", "")),
        }
        for row in members
    ]
    normalized.sort(key=lambda row: row["ticker"])
    universe_digest = digest(normalized)
    universe_id = digest({"source_run_as_of": source_run_as_of, "capture_phase": capture_phase, "members": normalized})
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO capture_universes VALUES(?,?,?,?,?,?)",
            (universe_id, source_run_as_of, capture_phase, created_at_utc, len(normalized), universe_digest),
        )
        for row in normalized:
            ident = ensure_instrument(conn, ticker=row["ticker"], providers=providers)
            conn.execute(
                "INSERT OR IGNORE INTO capture_universe_members VALUES(?,?,?,?,?,?)",
                (universe_id, ident, row["ticker"], row["tier"], row["sector"], row["source_pipeline"]),
            )
    return universe_id


def register_provider_universe(
    conn: sqlite3.Connection,
    *,
    source_run_as_of: str,
    members: Sequence[Mapping[str, Any]],
    providers: Sequence[str],
    source_artifact_path: str,
    source_artifact_sha256: str,
    activated_at_utc: str,
) -> str:
    """Append one sealed provider-owned universe version without replacing history."""
    date.fromisoformat(source_run_as_of)
    normalized = [
        {
            "ticker": str(row["ticker"]).strip().upper(),
            "tier": str(row.get("tier", "")).strip().casefold(),
            "sector": str(row.get("sector", "")),
            "source_pipeline": str(row.get("source_pipeline", "")),
        }
        for row in members
        if str(row.get("ticker", "")).strip().upper() != "CASH"
    ]
    normalized.sort(key=lambda row: row["ticker"])
    tickers = [row["ticker"] for row in normalized]
    if not normalized or len(tickers) != len(set(tickers)):
        raise ValueError("Provider universe registry must be non-empty and ticker-unique")
    invalid_tiers = sorted({row["tier"] for row in normalized} - {"tier0", "tier1", "tier2"})
    if invalid_tiers:
        raise ValueError(f"Invalid provider universe tiers: {invalid_tiers}")
    universe_digest = digest(normalized)
    registry_id = digest(
        {
            "source_run_as_of": source_run_as_of,
            "universe_digest": universe_digest,
        }
    )
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO provider_universe_registry VALUES(?,?,?,?,?,?,?)",
            (
                registry_id,
                source_run_as_of,
                activated_at_utc,
                source_artifact_path,
                source_artifact_sha256,
                len(normalized),
                universe_digest,
            ),
        )
        for row in normalized:
            ident = ensure_instrument(conn, ticker=row["ticker"], providers=providers)
            conn.execute(
                "INSERT OR IGNORE INTO provider_universe_registry_members VALUES(?,?,?,?,?,?)",
                (
                    registry_id,
                    ident,
                    row["ticker"],
                    row["tier"],
                    row["sector"],
                    row["source_pipeline"],
                ),
            )
    return registry_id


def load_provider_universe(
    conn: sqlite3.Connection,
    *,
    tiers: set[str],
    actual_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the latest provider-owned registry that existed by the capture date."""
    registry = conn.execute(
        "SELECT * FROM provider_universe_registry WHERE source_run_as_of<=? "
        "ORDER BY source_run_as_of DESC, rowid DESC LIMIT 1",
        (actual_date.isoformat(),),
    ).fetchone()
    if registry is None:
        raise ValueError("Provider universe registry is empty; sync a sealed universe first")
    invalid_tiers = tiers - {"tier0", "tier1", "tier2"}
    if not tiers or invalid_tiers:
        raise ValueError(f"Invalid requested provider universe tiers: {sorted(tiers)}")
    placeholders = ",".join("?" for _ in tiers)
    rows = conn.execute(
        "SELECT ticker,tier,sector,source_pipeline "
        "FROM provider_universe_registry_members "
        f"WHERE registry_id=? AND tier IN ({placeholders}) ORDER BY ticker",
        (str(registry["registry_id"]), *sorted(tiers)),
    ).fetchall()
    return dict(registry), [dict(row) for row in rows]


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must be timezone-aware: {value}")
    return parsed.astimezone(timezone.utc)


def _calendar_session(
    calendar_name: str,
    value: date,
    *,
    direction: Literal["next", "previous", "none"],
) -> tuple[Any, Any]:
    import exchange_calendars as xcals  # type: ignore[import-untyped]
    import pandas as pd  # type: ignore[import-untyped]

    calendar = xcals.get_calendar(calendar_name)
    return calendar, calendar.date_to_session(cast(Any, pd.Timestamp(value)), direction=direction)


def actionability(
    *,
    response_received_at_utc: str,
    cycle_completed_at_utc: str,
    timezone_name: str,
    calendar_name: str,
    decision_cutoff_local: str,
) -> dict[str, Any]:
    """Map an observation to the first session where a decision may use it."""
    received = _parse_utc(response_received_at_utc)
    completed = _parse_utc(cycle_completed_at_utc)
    zone = ZoneInfo(timezone_name)
    local_received = received.astimezone(zone)
    hour, minute = (int(value) for value in decision_cutoff_local.split(":"))
    local_date = local_received.date()
    calendar, session = _calendar_session(calendar_name, local_date, direction="next")
    is_session = session.date() == local_date
    cutoff_local = datetime.combine(local_date, wall_time(hour, minute), tzinfo=zone)
    cutoff_utc = cutoff_local.astimezone(timezone.utc)
    same_session = is_session and received <= cutoff_utc and completed <= cutoff_utc
    if same_session:
        effective_session = session
        effective_from = max(received, completed)
    else:
        effective_session = calendar.next_session(session) if is_session else session
        effective_from = calendar.session_open(effective_session).to_pydatetime().astimezone(timezone.utc)
    return {
        "effective_trading_date": effective_session.date().isoformat(),
        "effective_from_utc": effective_from.replace(microsecond=0).isoformat(),
        "same_session_eligible": int(same_session),
        "decision_cutoff_utc": cutoff_utc.replace(microsecond=0).isoformat(),
    }


def reject_historical_current_capture(
    *, requested_portfolio_as_of: date | None, now_utc: datetime, timezone_name: str
) -> None:
    if requested_portfolio_as_of is None:
        return
    current_local_date = now_utc.astimezone(ZoneInfo(timezone_name)).date()
    if requested_portfolio_as_of != current_local_date:
        raise ValueError(
            "Current-snapshot provider endpoints cannot be queried for a historical or future "
            f"portfolio date: requested={requested_portfolio_as_of}; actual={current_local_date}"
        )


def _optional_number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None or str(value).strip() == "":
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Provider estimate value must be finite: {value!r}")
    if integer:
        if number < 0 or not number.is_integer():
            raise ValueError(f"Provider count must be a non-negative integer: {value!r}")
        return int(number)
    return number


def _version_payload(row: Mapping[str, Any], *, ident: str, provider_symbol: str) -> dict[str, Any]:
    estimate_type = str(row["estimate_type"])
    metric = estimate_type.split("_", 1)[0]
    units = "currency_per_share" if metric == "eps" else "currency_units"
    semantic = {
        "provider": str(row["provider"]),
        "estimate_type": estimate_type,
        "currency": str(row.get("currency", "")),
        "units": units,
        "split_basis": "provider_defined_unknown",
        "estimate_definition": "provider_consensus_unknown_gaap_adjusted",
    }
    natural = {
        "provider": str(row["provider"]),
        "endpoint_id": str(row["endpoint_id"]),
        "instrument_id": ident,
        "fiscal_period_end": str(row["fiscal_period_end"]),
        "fiscal_period": str(row["fiscal_period"]),
        "estimate_type": estimate_type,
        "semantic_basis_hash": digest(semantic),
    }
    content = {
        field: _optional_number(row.get(field), integer=(field == "analyst_count" or "revision_" in field))
        for field in VERSION_VALUE_FIELDS
    }
    return {
        **natural,
        **semantic,
        **content,
        "natural_key_hash": digest(natural),
        "normalized_content_sha256": digest(content),
        "ticker": str(row["ticker"]).strip().upper(),
        "provider_symbol": provider_symbol,
    }


def _latest_observation(conn: sqlite3.Connection, natural_key_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT o.*,v.normalized_content_sha256,v.estimate_average,v.version_id "
        "FROM estimate_observations o JOIN estimate_versions v ON v.version_id=o.version_id "
        "JOIN capture_runs cr ON cr.run_id=o.run_id "
        "WHERE v.natural_key_hash=? AND cr.status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') "
        "ORDER BY o.available_at_utc DESC,o.observation_id DESC LIMIT 1",
        (natural_key_hash,),
    ).fetchone()


def _insert_version(
    conn: sqlite3.Connection,
    *,
    version: Mapping[str, Any],
    version_id: str,
    created_at_utc: str,
) -> bool:
    if conn.execute("SELECT 1 FROM estimate_versions WHERE version_id=?", (version_id,)).fetchone():
        return False
    columns = (
        "version_id",
        "natural_key_hash",
        "normalized_content_sha256",
        "provider",
        "endpoint_id",
        "instrument_id",
        "ticker",
        "provider_symbol",
        "fiscal_period_end",
        "fiscal_period",
        "estimate_type",
        "currency",
        "units",
        "split_basis",
        "estimate_definition",
        "semantic_basis_hash",
        *VERSION_VALUE_FIELDS,
        "created_at_utc",
    )
    values = (version_id, *(version[column] for column in columns[1:-1]), created_at_utc)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO estimate_versions({','.join(columns)}) VALUES({placeholders})",
        values,
    )
    return True


def _changed_fields(
    conn: sqlite3.Connection,
    *,
    prior_version_id: str,
    version: Mapping[str, Any],
) -> list[str]:
    columns = ",".join(VERSION_VALUE_FIELDS)
    prior = conn.execute(f"SELECT {columns} FROM estimate_versions WHERE version_id=?", (prior_version_id,)).fetchone()
    if prior is None:
        raise ValueError(f"Missing prior estimate version: {prior_version_id}")
    return [field for field in VERSION_VALUE_FIELDS if prior[field] != version[field]]


def _run_identity(
    *,
    run_id: str,
    cycle_id: str,
    capture_phase: str,
    actual_capture_date: str,
    universe_id: str,
    started_at_utc: str,
    completed_at_utc: str,
    status: str,
    previous_digest: str,
    request_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "cycle_id": cycle_id,
        "capture_phase": capture_phase,
        "actual_capture_date": actual_capture_date,
        "universe_id": universe_id,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "status": status,
        "previous_pass_digest": previous_digest,
        "requests": [
            {
                "provider": row["provider"],
                "endpoint_id": row["endpoint_id"],
                "ticker": row["ticker"],
                "status": row["status"],
                "response_sha256": row.get("response_sha256", ""),
                "response_received_at_utc": row["response_received_at_utc"],
                "normalized_digest": digest(row.get("normalized_rows", [])),
            }
            for row in request_records
        ],
    }


def _stored_run_digest(conn: sqlite3.Connection, run_id: str) -> str:
    run = conn.execute("SELECT * FROM capture_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Unknown capture run: {run_id}")
    run_fields = {key: run[key] for key in run.keys() if key != "run_digest"}
    requests = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM capture_requests WHERE run_id=? ORDER BY provider,endpoint_id,provider_symbol,request_id",
            (run_id,),
        ).fetchall()
    ]
    observations = [
        dict(row)
        for row in conn.execute(
            "SELECT observation_id,request_id,version_id,observation_digest "
            "FROM estimate_observations WHERE run_id=? ORDER BY observation_id",
            (run_id,),
        ).fetchall()
    ]
    return digest(
        {
            "schema_version": "provider_capture_run_digest_v2",
            "run": run_fields,
            "requests": requests,
            "observations": observations,
        }
    )


def _insert_request_observations(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    cycle_started_at_utc: str,
    completed_at_utc: str,
    request: Mapping[str, Any],
    timezone_name: str,
    calendar_name: str,
    decision_cutoff_local: str,
    record_changes: bool,
) -> tuple[int, int]:
    provider = str(request["provider"]).strip()
    ticker = str(request["ticker"]).strip().upper()
    provider_symbol = str(request.get("provider_symbol", ticker)).strip().upper()
    if not provider or not ticker or not provider_symbol:
        raise ValueError("Provider request identity fields must be non-empty")
    endpoint_id = str(request["endpoint_id"]).strip()
    if not endpoint_id:
        raise ValueError("Provider request endpoint_id must be non-empty")
    request_started = _parse_utc(str(request["request_started_at_utc"]))
    response_received = _parse_utc(str(request["response_received_at_utc"]))
    cycle_started = _parse_utc(cycle_started_at_utc)
    cycle_completed = _parse_utc(completed_at_utc)
    if not cycle_started <= request_started <= response_received <= cycle_completed:
        raise ValueError("Provider request timestamps are not monotone within the capture cycle")
    http_status_raw = request.get("http_status")
    http_status = None if http_status_raw in (None, "") else int(http_status_raw)
    if http_status is not None and not 100 <= http_status <= 599:
        raise ValueError("Provider HTTP status must be between 100 and 599")
    elapsed_ms = int(request.get("elapsed_ms", 0))
    provider_row_count = int(request.get("provider_row_count", 0))
    if elapsed_ms < 0 or provider_row_count < 0:
        raise ValueError("Provider request counts and elapsed time must be non-negative")
    request_status = str(request["status"])
    response_sha256 = str(request.get("response_sha256", ""))
    if request_status in CLEAN_REQUEST_STATUSES and not _is_sha256(response_sha256):
        raise ValueError("Clean provider response must have a valid SHA-256 digest")

    ident = ensure_instrument(conn, ticker=ticker, providers=(provider,))
    conn.execute(
        "INSERT INTO provider_symbols(instrument_id,provider,provider_symbol) VALUES(?,?,?) "
        "ON CONFLICT(instrument_id,provider,valid_from) DO UPDATE SET "
        "provider_symbol=excluded.provider_symbol",
        (ident, provider, provider_symbol),
    )
    request_id = digest(
        {
            "run_id": run_id,
            "provider": provider,
            "endpoint_id": endpoint_id,
            "provider_symbol": provider_symbol,
        }
    )
    normalized_rows = list(request.get("normalized_rows", []))
    if normalized_rows and request_status != "AVAILABLE":
        raise ValueError("Only AVAILABLE requests may contain normalized rows")
    if request_status == "AVAILABLE" and not normalized_rows:
        raise ValueError("AVAILABLE request must contain normalized rows")

    prepared_versions: dict[str, dict[str, Any]] = {}
    for raw in normalized_rows:
        if str(raw.get("provider", "")).strip() != provider:
            raise ValueError("Normalized row provider differs from its request")
        if str(raw.get("endpoint_id", "")).strip() != endpoint_id:
            raise ValueError("Normalized row endpoint differs from its request")
        if str(raw.get("ticker", "")).strip().upper() != ticker:
            raise ValueError("Normalized row ticker differs from its request")
        date.fromisoformat(str(raw.get("fiscal_period_end", "")))
        version = _version_payload(raw, ident=ident, provider_symbol=provider_symbol)
        natural_key = str(version["natural_key_hash"])
        prior_in_response = prepared_versions.get(natural_key)
        if prior_in_response is not None:
            qualifier = (
                "conflicting versions"
                if prior_in_response["normalized_content_sha256"] != version["normalized_content_sha256"]
                else "duplicate rows"
            )
            raise ValueError(
                f"Provider response contains {qualifier} for one estimate key: "
                f"{provider}/{ticker}/{version['estimate_type']}/{version['fiscal_period_end']}"
            )
        prepared_versions[natural_key] = version

    request_columns = (
        "request_id",
        "run_id",
        "instrument_id",
        "ticker",
        "provider",
        "provider_symbol",
        "endpoint_id",
        "request_started_at_utc",
        "response_received_at_utc",
        "status",
        "http_status",
        "elapsed_ms",
        "provider_row_count",
        "normalized_row_count",
        "response_sha256",
        "detail",
    )
    request_values = (
        request_id,
        run_id,
        ident,
        ticker,
        provider,
        provider_symbol,
        endpoint_id,
        str(request["request_started_at_utc"]),
        str(request["response_received_at_utc"]),
        request_status,
        http_status,
        elapsed_ms,
        provider_row_count,
        len(normalized_rows),
        response_sha256,
        str(request.get("detail", "")),
    )
    conn.execute(
        f"INSERT INTO capture_requests({','.join(request_columns)}) VALUES({','.join('?' for _ in request_columns)})",
        request_values,
    )
    new_versions = 0
    unchanged = 0
    action = actionability(
        response_received_at_utc=str(request["response_received_at_utc"]),
        cycle_completed_at_utc=completed_at_utc,
        timezone_name=timezone_name,
        calendar_name=calendar_name,
        decision_cutoff_local=decision_cutoff_local,
    )
    for version in prepared_versions.values():
        prior = _latest_observation(conn, str(version["natural_key_hash"]))
        version_id = digest(
            {
                "natural_key_hash": version["natural_key_hash"],
                "normalized_content_sha256": version["normalized_content_sha256"],
            }
        )
        new_versions += int(
            _insert_version(
                conn,
                version=version,
                version_id=version_id,
                created_at_utc=str(request["response_received_at_utc"]),
            )
        )
        is_unchanged = prior is not None and str(prior["version_id"]) == version_id
        unchanged += int(is_unchanged)
        observation_id = digest({"run_id": run_id, "request_id": request_id, "version_id": version_id})
        observation_digest = digest(
            {
                "observation_id": observation_id,
                "version_id": version_id,
                "available_at_utc": request["response_received_at_utc"],
                "effective_trading_date": action["effective_trading_date"],
                "prior_observation_id": "" if prior is None else prior["observation_id"],
            }
        )
        conn.execute(
            "INSERT INTO estimate_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                run_id,
                request_id,
                version_id,
                str(request["response_received_at_utc"]),
                str(request["response_received_at_utc"]),
                action["effective_trading_date"],
                action["effective_from_utc"],
                action["same_session_eligible"],
                int(is_unchanged),
                None if prior is None else prior["observation_id"],
                observation_digest,
            ),
        )
        if prior is None or is_unchanged or not record_changes:
            continue
        before = prior["estimate_average"]
        after = version["estimate_average"]
        changed_fields = _changed_fields(
            conn,
            prior_version_id=str(prior["version_id"]),
            version=version,
        )
        change_id = digest({"prior": prior["observation_id"], "new": observation_id})
        conn.execute(
            "INSERT INTO estimate_changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                change_id,
                version["natural_key_hash"],
                provider,
                ident,
                ticker,
                version["estimate_type"],
                version["fiscal_period_end"],
                prior["observation_id"],
                observation_id,
                prior["version_id"],
                version_id,
                prior["available_at_utc"],
                str(request["response_received_at_utc"]),
                before,
                after,
                None if before is None or after is None else float(after) - float(before),
                json.dumps(changed_fields),
            ),
        )
    return new_versions, unchanged


def _insert_coverage(
    conn: sqlite3.Connection,
    *,
    actual_capture_date: str,
    run_id: str,
    request_records: Sequence[Mapping[str, Any]],
) -> None:
    coverage: dict[tuple[str, str], dict[str, int | str]] = {}
    for request in request_records:
        provider = str(request["provider"])
        ticker = str(request["ticker"]).strip().upper()
        ident = instrument_id(ticker)
        key = (provider, ident)
        summary = coverage.setdefault(key, {"requests": 0, "available": 0, "errors": 0, "ticker": ticker})
        status = str(request["status"])
        summary["requests"] = int(summary["requests"]) + 1
        summary["available"] = int(summary["available"]) + int(status == "AVAILABLE")
        summary["errors"] = int(summary["errors"]) + int(status not in {"AVAILABLE", "EMPTY"})
    for (provider, ident), summary in coverage.items():
        errors = int(summary["errors"])
        available = int(summary["available"])
        status = "ERROR" if errors else "AVAILABLE" if available else "EMPTY"
        conn.execute(
            "INSERT INTO coverage_daily VALUES(?,?,?,?,?,?,?,?,?)",
            (
                actual_capture_date,
                run_id,
                provider,
                ident,
                summary["ticker"],
                status,
                summary["requests"],
                available,
                errors,
            ),
        )


def persist_capture(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    capture_phase: str,
    requested_portfolio_as_of: str,
    actual_capture_date: str,
    universe_id: str,
    started_at_utc: str,
    completed_at_utc: str,
    request_records: Sequence[Mapping[str, Any]],
    source_code_digest: str,
    config_digest: str,
    timezone_name: str,
    calendar_name: str,
    decision_cutoff_local: str,
    status: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically append one capture run and all normalized observations."""
    if status not in {"PASS", "PASS_WITH_WARNINGS", "FAIL", "MIGRATED"}:
        raise ValueError(f"Unsupported capture status: {status}")
    if not request_records:
        raise ValueError("Provider capture must contain at least one request")
    date.fromisoformat(actual_capture_date)
    started = _parse_utc(started_at_utc)
    completed = _parse_utc(completed_at_utc)
    if completed < started:
        raise ValueError("Provider capture completion precedes its start")
    if not cycle_id.strip() or not capture_phase.strip():
        raise ValueError("Provider capture identity fields must be non-empty")
    incoming_request_digest = digest(
        [
            {
                "provider": row["provider"],
                "endpoint_id": row["endpoint_id"],
                "ticker": row["ticker"],
                "status": row["status"],
                "request_started_at_utc": row["request_started_at_utc"],
                "response_received_at_utc": row["response_received_at_utc"],
                "response_sha256": row.get("response_sha256", ""),
                "normalized_digest": digest(row.get("normalized_rows", [])),
            }
            for row in request_records
        ]
    )
    existing = conn.execute(
        "SELECT * FROM capture_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if existing is not None:
        existing_metadata = json.loads(str(existing["metadata_json"] or "{}"))
        existing_input_digest = str(existing_metadata.get("input_request_digest", ""))
        identity = {
            "capture_phase": capture_phase,
            "requested_portfolio_as_of": requested_portfolio_as_of,
            "actual_capture_date": actual_capture_date,
            "universe_id": universe_id,
            "started_at_utc": started_at_utc,
            "completed_at_utc": completed_at_utc,
            "status": status,
        }
        mismatched_identity = [field for field, value in identity.items() if str(existing[field]) != str(value)]
        if mismatched_identity:
            raise ValueError(
                f"Capture cycle {cycle_id!r} already exists with different identity: {mismatched_identity}"
            )
        if existing_input_digest != incoming_request_digest:
            raise ValueError(f"Capture cycle {cycle_id!r} already exists with different request content")
        return {
            "idempotent": True,
            "run_digest": str(existing["run_digest"]),
            "status": str(existing["status"]),
        }
    run_id = digest({"cycle_id": cycle_id, "universe_id": universe_id})
    prior = conn.execute(
        "SELECT run_digest FROM capture_runs "
        "WHERE status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    previous_digest = str(prior["run_digest"]) if prior is not None else EMPTY_DIGEST
    available = sum(str(row["status"]) == "AVAILABLE" for row in request_records)
    empty = sum(str(row["status"]) == "EMPTY" for row in request_records)
    errors = len(request_records) - available - empty
    normalized_count = sum(len(row.get("normalized_rows", [])) for row in request_records)
    run_identity = _run_identity(
        run_id=run_id,
        cycle_id=cycle_id,
        capture_phase=capture_phase,
        actual_capture_date=actual_capture_date,
        universe_id=universe_id,
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        status=status,
        previous_digest=previous_digest,
        request_records=request_records,
    )
    run_digest = digest(run_identity)
    action = actionability(
        response_received_at_utc=completed_at_utc,
        cycle_completed_at_utc=completed_at_utc,
        timezone_name=timezone_name,
        calendar_name=calendar_name,
        decision_cutoff_local=decision_cutoff_local,
    )
    run_columns = (
        "run_id",
        "cycle_id",
        "capture_phase",
        "requested_portfolio_as_of",
        "actual_capture_date",
        "universe_id",
        "started_at_utc",
        "completed_at_utc",
        "decision_cutoff_utc",
        "status",
        "request_count",
        "available_request_count",
        "empty_request_count",
        "error_request_count",
        "normalized_row_count",
        "new_version_count",
        "unchanged_observation_count",
        "previous_pass_digest",
        "run_digest",
        "source_code_digest",
        "config_digest",
        "metadata_json",
    )
    stored_metadata = dict(metadata or {})
    stored_metadata["input_request_digest"] = incoming_request_digest
    run_values = (
        run_id,
        cycle_id,
        capture_phase,
        requested_portfolio_as_of,
        actual_capture_date,
        universe_id,
        started_at_utc,
        completed_at_utc,
        action["decision_cutoff_utc"],
        status,
        len(request_records),
        available,
        empty,
        errors,
        normalized_count,
        0,
        0,
        previous_digest,
        run_digest,
        source_code_digest,
        config_digest,
        json.dumps(stored_metadata, sort_keys=True),
    )
    new_versions = 0
    unchanged = 0
    with conn:
        conn.execute(
            f"INSERT INTO capture_runs({','.join(run_columns)}) VALUES({','.join('?' for _ in run_columns)})",
            run_values,
        )
        for request in request_records:
            inserted, same = _insert_request_observations(
                conn,
                run_id=run_id,
                cycle_started_at_utc=started_at_utc,
                completed_at_utc=completed_at_utc,
                request=request,
                timezone_name=timezone_name,
                calendar_name=calendar_name,
                decision_cutoff_local=decision_cutoff_local,
                record_changes=status in {"PASS", "PASS_WITH_WARNINGS", "MIGRATED"},
            )
            new_versions += inserted
            unchanged += same
        _insert_coverage(
            conn,
            actual_capture_date=actual_capture_date,
            run_id=run_id,
            request_records=request_records,
        )
        conn.execute(
            "UPDATE capture_runs SET new_version_count=?,unchanged_observation_count=? WHERE run_id=?",
            (new_versions, unchanged, run_id),
        )
        if status != "MIGRATED":
            run_digest = _stored_run_digest(conn, run_id)
            conn.execute("UPDATE capture_runs SET run_digest=? WHERE run_id=?", (run_digest, run_id))
    return {
        "idempotent": False,
        "run_id": run_id,
        "run_digest": run_digest,
        "status": status,
        "request_count": len(request_records),
        "normalized_row_count": normalized_count,
        "new_version_count": new_versions,
        "unchanged_observation_count": unchanged,
        "previous_pass_digest": previous_digest,
    }


def record_artifact_dependencies(
    conn: sqlite3.Connection,
    *,
    artifact_path: str,
    artifact_sha256: str,
    observation_ids: Iterable[str],
) -> None:
    now = utc_now()
    ids = sorted(set(observation_ids))
    if not ids:
        raise ValueError("Provider artifact must depend on at least one observation")
    with conn:
        conn.execute(
            "UPDATE artifact_dependencies SET status='superseded' WHERE artifact_path=? AND status='active'",
            (artifact_path,),
        )
        for observation_id in ids:
            found = conn.execute(
                "SELECT 1 FROM estimate_observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if found is None:
                raise ValueError(f"Unknown provider observation dependency: {observation_id}")
            conn.execute(
                "INSERT OR REPLACE INTO artifact_dependencies VALUES(?,?,?,?, 'active')",
                (artifact_path, artifact_sha256, observation_id, now),
            )


def supersede_artifact_dependencies(conn: sqlite3.Connection, *, artifact_path: str) -> None:
    """Retire the currently active lineage for an artifact path."""
    with conn:
        conn.execute(
            "UPDATE artifact_dependencies SET status='superseded' WHERE artifact_path=? AND status='active'",
            (artifact_path,),
        )


def artifact_dependency_errors(conn: sqlite3.Connection, *, artifact_path: str, artifact_sha256: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.observation_id,o.observation_digest FROM artifact_dependencies d "
        "LEFT JOIN estimate_observations o ON o.observation_id=d.observation_id "
        "WHERE d.artifact_path=? AND d.artifact_sha256=? AND d.status='active'",
        (artifact_path, artifact_sha256),
    ).fetchall()
    if not rows:
        return ["no_active_dependencies"]
    return [f"missing_observation:{row['observation_id']}" for row in rows if not row["observation_digest"]]


def _run_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    runs = conn.execute("SELECT * FROM capture_runs ORDER BY rowid").fetchall()
    for row in runs:
        run_id = str(row["run_id"])
        expected_id = digest(
            {
                "cycle_id": str(row["cycle_id"]),
                "universe_id": str(row["universe_id"]),
            }
        )
        if run_id != expected_id:
            errors.append(f"run_id_mismatch:{run_id}")
        try:
            started = _parse_utc(str(row["started_at_utc"]))
            completed = _parse_utc(str(row["completed_at_utc"]))
            _parse_utc(str(row["decision_cutoff_utc"]))
            date.fromisoformat(str(row["actual_capture_date"]))
            requested = str(row["requested_portfolio_as_of"])
            if requested:
                date.fromisoformat(requested)
            if completed < started:
                errors.append(f"run_completion_before_start:{run_id}")
        except ValueError:
            errors.append(f"run_date_invalid:{run_id}")
        if not _is_sha256(str(row["source_code_digest"])):
            errors.append(f"run_source_digest_invalid:{run_id}")
        if not _is_sha256(str(row["config_digest"])):
            errors.append(f"run_config_digest_invalid:{run_id}")
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError:
            errors.append(f"run_metadata_invalid:{run_id}")
        else:
            if not isinstance(metadata, dict):
                errors.append(f"run_metadata_not_mapping:{run_id}")
        count_fields = (
            "request_count",
            "available_request_count",
            "empty_request_count",
            "error_request_count",
            "normalized_row_count",
            "new_version_count",
            "unchanged_observation_count",
        )
        if any(int(row[field]) < 0 for field in count_fields):
            errors.append(f"run_count_negative:{run_id}")
    return errors


def _version_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    for row in conn.execute("SELECT * FROM estimate_versions ORDER BY version_id"):
        values = dict(row)
        semantic = {
            "provider": str(values["provider"]),
            "estimate_type": str(values["estimate_type"]),
            "currency": str(values["currency"]),
            "units": str(values["units"]),
            "split_basis": str(values["split_basis"]),
            "estimate_definition": str(values["estimate_definition"]),
        }
        semantic_hash = digest(semantic)
        if str(values["semantic_basis_hash"]) != semantic_hash:
            errors.append(f"version_semantic_digest_mismatch:{values['version_id']}")
        natural = {
            "provider": str(values["provider"]),
            "endpoint_id": str(values["endpoint_id"]),
            "instrument_id": str(values["instrument_id"]),
            "fiscal_period_end": str(values["fiscal_period_end"]),
            "fiscal_period": str(values["fiscal_period"]),
            "estimate_type": str(values["estimate_type"]),
            "semantic_basis_hash": semantic_hash,
        }
        natural_hash = digest(natural)
        if str(values["natural_key_hash"]) != natural_hash:
            errors.append(f"version_natural_key_mismatch:{values['version_id']}")
        content = {field: values[field] for field in VERSION_VALUE_FIELDS}
        content_hash = digest(content)
        if str(values["normalized_content_sha256"]) != content_hash:
            errors.append(f"version_content_digest_mismatch:{values['version_id']}")
        expected_version_id = digest(
            {
                "natural_key_hash": natural_hash,
                "normalized_content_sha256": content_hash,
            }
        )
        if str(values["version_id"]) != expected_version_id:
            errors.append(f"version_id_mismatch:{values['version_id']}")
        ticker = str(values["ticker"])
        if ticker != ticker.strip().upper() or not ticker:
            errors.append(f"version_ticker_invalid:{values['version_id']}")
        if str(values["instrument_id"]) != instrument_id(ticker):
            errors.append(f"version_instrument_mismatch:{values['version_id']}")
        for field in VERSION_VALUE_FIELDS:
            value = values[field]
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"version_value_invalid:{values['version_id']}:{field}")
                continue
            if not math.isfinite(number):
                errors.append(f"version_value_nonfinite:{values['version_id']}:{field}")
            if (field == "analyst_count" or "revision_" in field) and (number < 0 or not number.is_integer()):
                errors.append(f"version_count_invalid:{values['version_id']}:{field}")
        try:
            date.fromisoformat(str(values["fiscal_period_end"]))
            _parse_utc(str(values["created_at_utc"]))
        except ValueError:
            errors.append(f"version_date_invalid:{values['version_id']}")
    return errors


def _universe_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    instruments = conn.execute(
        "SELECT instrument_id,canonical_ticker FROM instruments ORDER BY instrument_id"
    ).fetchall()
    for row in instruments:
        ticker = str(row["canonical_ticker"])
        if ticker != ticker.strip().upper() or str(row["instrument_id"]) != instrument_id(ticker):
            errors.append(f"instrument_identity_mismatch:{row['instrument_id']}")
    provider_symbols = conn.execute(
        "SELECT instrument_id,provider,provider_symbol FROM provider_symbols ORDER BY instrument_id,provider,valid_from"
    ).fetchall()
    for row in provider_symbols:
        if not str(row["provider"]).strip() or not str(row["provider_symbol"]).strip():
            errors.append(f"provider_symbol_invalid:{row['instrument_id']}:{row['provider']}")

    universes = conn.execute("SELECT * FROM capture_universes ORDER BY rowid").fetchall()
    for universe in universes:
        member_rows = conn.execute(
            "SELECT instrument_id,ticker,tier,sector,source_pipeline "
            "FROM capture_universe_members WHERE universe_id=? ORDER BY ticker",
            (str(universe["universe_id"]),),
        ).fetchall()
        members = [
            {
                "ticker": str(row["ticker"]),
                "tier": str(row["tier"]),
                "sector": str(row["sector"]),
                "source_pipeline": str(row["source_pipeline"]),
            }
            for row in member_rows
        ]
        for row in member_rows:
            if str(row["instrument_id"]) != instrument_id(str(row["ticker"])):
                errors.append(f"universe_member_instrument_mismatch:{universe['universe_id']}:{row['ticker']}")
        if len(members) != int(universe["member_count"]):
            errors.append(f"universe_member_count_mismatch:{universe['universe_id']}")
        universe_digest = digest(members)
        if universe_digest != str(universe["universe_digest"]):
            errors.append(f"universe_digest_mismatch:{universe['universe_id']}")
        expected_id = digest(
            {
                "source_run_as_of": str(universe["source_run_as_of"]),
                "capture_phase": str(universe["capture_phase"]),
                "members": members,
            }
        )
        if expected_id != str(universe["universe_id"]):
            errors.append(f"universe_id_mismatch:{universe['universe_id']}")

    registries = conn.execute("SELECT * FROM provider_universe_registry ORDER BY rowid").fetchall()
    for registry in registries:
        member_rows = conn.execute(
            "SELECT instrument_id,ticker,tier,sector,source_pipeline "
            "FROM provider_universe_registry_members "
            "WHERE registry_id=? ORDER BY ticker",
            (str(registry["registry_id"]),),
        ).fetchall()
        members = [
            {
                "ticker": str(row["ticker"]),
                "tier": str(row["tier"]),
                "sector": str(row["sector"]),
                "source_pipeline": str(row["source_pipeline"]),
            }
            for row in member_rows
        ]
        for row in member_rows:
            if str(row["instrument_id"]) != instrument_id(str(row["ticker"])):
                errors.append(f"registry_member_instrument_mismatch:{registry['registry_id']}:{row['ticker']}")
        if len(members) != int(registry["member_count"]):
            errors.append(f"registry_member_count_mismatch:{registry['registry_id']}")
        universe_digest = digest(members)
        if universe_digest != str(registry["universe_digest"]):
            errors.append(f"registry_digest_mismatch:{registry['registry_id']}")
        expected_id = digest(
            {
                "source_run_as_of": str(registry["source_run_as_of"]),
                "universe_digest": universe_digest,
            }
        )
        if expected_id != str(registry["registry_id"]):
            errors.append(f"registry_id_mismatch:{registry['registry_id']}")
        if not _is_sha256(str(registry["source_artifact_sha256"])):
            errors.append(f"registry_source_hash_invalid:{registry['registry_id']}")
    return errors


def _coverage_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    expected: dict[tuple[str, str, str], dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT q.run_id,q.provider,q.instrument_id,q.ticker,q.status,"
        "cr.actual_capture_date FROM capture_requests q "
        "JOIN capture_runs cr ON cr.run_id=q.run_id "
        "ORDER BY q.run_id,q.provider,q.instrument_id"
    ).fetchall()
    for row in rows:
        key = (str(row["run_id"]), str(row["provider"]), str(row["instrument_id"]))
        item = expected.setdefault(
            key,
            {
                "coverage_date": str(row["actual_capture_date"]),
                "run_id": str(row["run_id"]),
                "provider": str(row["provider"]),
                "instrument_id": str(row["instrument_id"]),
                "ticker": str(row["ticker"]),
                "request_count": 0,
                "available_count": 0,
                "error_count": 0,
            },
        )
        item["request_count"] += 1
        status = str(row["status"])
        item["available_count"] += int(status == "AVAILABLE")
        item["error_count"] += int(status not in CLEAN_REQUEST_STATUSES)
    actual_rows = conn.execute("SELECT * FROM coverage_daily ORDER BY run_id,provider,instrument_id").fetchall()
    actual = {(str(row["run_id"]), str(row["provider"]), str(row["instrument_id"])): dict(row) for row in actual_rows}
    if set(actual) != set(expected):
        errors.append("coverage_key_set_mismatch")
    for key in sorted(set(actual) & set(expected)):
        wanted = expected[key]
        wanted["status"] = "ERROR" if wanted["error_count"] else "AVAILABLE" if wanted["available_count"] else "EMPTY"
        if actual[key] != wanted:
            errors.append(f"coverage_row_mismatch:{':'.join(key)}")
    return errors


def _change_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    value_columns = ",".join(f"pv.{field} AS prior_{field},nv.{field} AS new_{field}" for field in VERSION_VALUE_FIELDS)
    rows = conn.execute(
        "SELECT c.*,po.version_id AS observed_prior_version,"
        "po.available_at_utc AS observed_prior_at,"
        "no.version_id AS observed_new_version,no.available_at_utc AS observed_new_at,"
        "pv.natural_key_hash AS prior_natural,pv.estimate_average AS prior_average,"
        "nv.natural_key_hash AS new_natural,nv.provider AS new_provider,"
        "nv.instrument_id AS new_instrument,nv.ticker AS new_ticker,"
        "nv.estimate_type AS new_type,nv.fiscal_period_end AS new_period,"
        "nv.estimate_average AS new_average," + value_columns + " FROM estimate_changes c "
        "LEFT JOIN estimate_observations po ON po.observation_id=c.prior_observation_id "
        "LEFT JOIN estimate_observations no ON no.observation_id=c.new_observation_id "
        "LEFT JOIN estimate_versions pv ON pv.version_id=c.prior_version_id "
        "LEFT JOIN estimate_versions nv ON nv.version_id=c.new_version_id "
        "ORDER BY c.change_id"
    )
    for row in rows:
        change_id = str(row["change_id"])
        if change_id != digest({"prior": row["prior_observation_id"], "new": row["new_observation_id"]}):
            errors.append(f"change_id_mismatch:{change_id}")
        expected_pairs = {
            "prior_version_id": row["observed_prior_version"],
            "new_version_id": row["observed_new_version"],
            "interval_start_utc": row["observed_prior_at"],
            "interval_end_utc": row["observed_new_at"],
            "natural_key_hash": row["new_natural"],
            "provider": row["new_provider"],
            "instrument_id": row["new_instrument"],
            "ticker": row["new_ticker"],
            "estimate_type": row["new_type"],
            "fiscal_period_end": row["new_period"],
        }
        for field, expected_value in expected_pairs.items():
            if row[field] != expected_value:
                errors.append(f"change_field_mismatch:{change_id}:{field}")
        if row["prior_natural"] != row["new_natural"]:
            errors.append(f"change_natural_key_mismatch:{change_id}")
        before = row["prior_average"]
        after = row["new_average"]
        if row["estimate_average_before"] != before or row["estimate_average_after"] != after:
            errors.append(f"change_average_mismatch:{change_id}")
        expected_delta = None if before is None or after is None else float(after) - float(before)
        delta = row["estimate_average_delta"]
        if expected_delta is None:
            if delta is not None:
                errors.append(f"change_delta_mismatch:{change_id}")
        elif delta is None or not math.isclose(float(delta), expected_delta, abs_tol=1e-12):
            errors.append(f"change_delta_mismatch:{change_id}")
        try:
            changed_fields = json.loads(str(row["changed_fields_json"]))
        except json.JSONDecodeError:
            errors.append(f"change_fields_invalid:{change_id}")
            continue
        expected_fields = [field for field in VERSION_VALUE_FIELDS if row[f"prior_{field}"] != row[f"new_{field}"]]
        if changed_fields != expected_fields:
            errors.append(f"change_fields_mismatch:{change_id}")
    missing = conn.execute(
        "SELECT o.observation_id FROM estimate_observations o "
        "JOIN capture_runs cr ON cr.run_id=o.run_id "
        "JOIN estimate_observations p ON p.observation_id=o.prior_observation_id "
        "LEFT JOIN estimate_changes c ON c.new_observation_id=o.observation_id "
        "WHERE cr.status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') "
        "AND o.version_id<>p.version_id AND c.change_id IS NULL LIMIT 100"
    ).fetchall()
    errors.extend(f"change_missing:{row['observation_id']}" for row in missing)
    return errors


def _artifact_integrity_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    duplicate_active = conn.execute(
        "SELECT artifact_path FROM artifact_dependencies WHERE status='active' "
        "GROUP BY artifact_path HAVING COUNT(DISTINCT artifact_sha256)<>1"
    ).fetchall()
    errors.extend(f"artifact_multiple_active_hashes:{row['artifact_path']}" for row in duplicate_active)
    active = conn.execute(
        "SELECT DISTINCT artifact_path,artifact_sha256 "
        "FROM artifact_dependencies WHERE status='active' ORDER BY artifact_path"
    ).fetchall()
    for row in active:
        artifact_path = str(row["artifact_path"])
        artifact_hash = str(row["artifact_sha256"])
        path = Path(artifact_path)
        if not path.is_absolute():
            errors.append(f"artifact_path_not_absolute:{artifact_path}")
        elif not path.is_file():
            errors.append(f"artifact_missing:{artifact_path}")
        elif not _is_sha256(artifact_hash) or source_digest(path) != artifact_hash:
            errors.append(f"artifact_hash_mismatch:{artifact_path}")
    annotations = conn.execute("SELECT * FROM legacy_migration_annotations ORDER BY run_id").fetchall()
    for row in annotations:
        identity = {
            "run_id": str(row["run_id"]),
            "legacy_retrieval_cycle": str(row["legacy_retrieval_cycle"]),
            "stated_as_of_date": str(row["stated_as_of_date"]),
            "observed_capture_date": str(row["observed_capture_date"]),
            "legacy_asof_mismatch": int(row["legacy_asof_mismatch"]),
            "annotation_version": str(row["annotation_version"]),
        }
        if digest(identity) != str(row["annotation_digest"]):
            errors.append(f"legacy_annotation_digest_mismatch:{row['run_id']}")
    return errors


def verify_store_head(conn: sqlite3.Connection) -> list[str]:
    """Run bounded integrity checks for scheduler no-op polls.

    Every capture validates the complete append-only store before its child can
    return success, and the nightly portfolio run performs the exhaustive
    validator again.  Polling that same million-row history every ten minutes is
    unnecessary and can overlap the next capture.  This check recomputes the
    newest run digest and verifies the newest dispatch and accepted-run chain
    edge, so a new or partially sealed head still fails closed.
    """
    errors: list[str] = []
    latest = conn.execute(
        "SELECT rowid AS storage_rowid,* FROM capture_runs WHERE status<>'MIGRATED' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if latest is not None:
        run_id = str(latest["run_id"])
        if str(latest["run_digest"]) != _stored_run_digest(conn, run_id):
            errors.append(f"run_digest_mismatch:{run_id}")
        previous = conn.execute(
            "SELECT run_digest FROM capture_runs "
            "WHERE status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') AND rowid<? "
            "ORDER BY rowid DESC LIMIT 1",
            (int(latest["storage_rowid"]),),
        ).fetchone()
        expected_previous = EMPTY_DIGEST if previous is None else str(previous["run_digest"])
        if str(latest["previous_pass_digest"]) != expected_previous:
            errors.append(f"run_chain_head_break:{run_id}")

    dispatch = conn.execute(
        "SELECT * FROM scheduled_dispatch_attempts ORDER BY started_at_utc DESC,cycle_id DESC LIMIT 1"
    ).fetchone()
    if dispatch is not None:
        cycle_id = str(dispatch["cycle_id"])
        if str(dispatch["attempt_digest"]) != digest(_dispatch_attempt_identity(dict(dispatch))):
            errors.append(f"dispatch_attempt_digest_mismatch:{cycle_id}")
        state = str(dispatch["state"])
        completed = str(dispatch["completed_at_utc"])
        if state == "STARTED" and completed:
            errors.append(f"dispatch_started_has_completion:{cycle_id}")
        if state != "STARTED" and not completed:
            errors.append(f"dispatch_terminal_missing_completion:{cycle_id}")
        if state == "PASS" and int(dispatch["return_code"] if dispatch["return_code"] is not None else -1) != 0:
            errors.append(f"dispatch_pass_return_code_invalid:{cycle_id}")
        artifact_path = str(dispatch["artifact_path"])
        artifact_sha = str(dispatch["artifact_sha256"])
        if bool(artifact_path) != bool(artifact_sha):
            errors.append(f"dispatch_artifact_contract_incomplete:{cycle_id}")
        elif artifact_path:
            path = Path(artifact_path)
            if not path.is_absolute():
                errors.append(f"dispatch_artifact_path_not_absolute:{cycle_id}")
            elif not path.is_file():
                errors.append(f"dispatch_artifact_missing:{cycle_id}")
            elif not _is_sha256(artifact_sha) or source_digest(path) != artifact_sha:
                errors.append(f"dispatch_artifact_hash_mismatch:{cycle_id}")
    return errors


def verify_store(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    dispatches = conn.execute("SELECT * FROM scheduled_dispatch_attempts ORDER BY started_at_utc,cycle_id")
    for row in dispatches:
        if str(row["attempt_digest"]) != digest(_dispatch_attempt_identity(dict(row))):
            errors.append(f"dispatch_attempt_digest_mismatch:{row['cycle_id']}")
        state = str(row["state"])
        if state == "STARTED" and str(row["completed_at_utc"]):
            errors.append(f"dispatch_started_has_completion:{row['cycle_id']}")
        if state != "STARTED" and not str(row["completed_at_utc"]):
            errors.append(f"dispatch_terminal_missing_completion:{row['cycle_id']}")
        try:
            started = _parse_utc(str(row["started_at_utc"]))
            completed_text = str(row["completed_at_utc"])
            if completed_text and _parse_utc(completed_text) < started:
                errors.append(f"dispatch_completion_before_start:{row['cycle_id']}")
        except ValueError:
            errors.append(f"dispatch_timestamp_invalid:{row['cycle_id']}")
        return_code = row["return_code"]
        if state == "PASS" and return_code != 0:
            errors.append(f"dispatch_pass_return_code_invalid:{row['cycle_id']}")
        if state == "PASS" and not str(row["artifact_path"]):
            errors.append(f"dispatch_pass_artifact_missing:{row['cycle_id']}")
        if state == "FAIL" and (return_code is None or int(return_code) == 0):
            errors.append(f"dispatch_fail_return_code_invalid:{row['cycle_id']}")
        if state == "INTERRUPTED" and return_code is not None:
            errors.append(f"dispatch_interrupted_return_code_invalid:{row['cycle_id']}")
        artifact_path = str(row["artifact_path"])
        artifact_sha = str(row["artifact_sha256"])
        if bool(artifact_path) != bool(artifact_sha):
            errors.append(f"dispatch_artifact_contract_incomplete:{row['cycle_id']}")
        elif artifact_path:
            path = Path(artifact_path)
            if not path.is_absolute():
                errors.append(f"dispatch_artifact_path_not_absolute:{row['cycle_id']}")
            elif not path.is_file():
                errors.append(f"dispatch_artifact_missing:{row['cycle_id']}")
            elif not _is_sha256(artifact_sha) or source_digest(path) != artifact_sha:
                errors.append(f"dispatch_artifact_hash_mismatch:{row['cycle_id']}")
    request_rows = conn.execute(
        "SELECT q.*,cr.started_at_utc AS cycle_started_at_utc,"
        "cr.completed_at_utc AS cycle_completed_at_utc,"
        "COALESCE(obs.observation_count,0) AS observation_count "
        "FROM capture_requests q JOIN capture_runs cr ON cr.run_id=q.run_id "
        # The observation uniqueness index is ordered (run_id, request_id,
        # version_id). Group on its full prefix so validation streams that
        # covering index instead of materializing a multi-gigabyte temp B-tree.
        "LEFT JOIN (SELECT run_id,request_id,COUNT(*) AS observation_count "
        "FROM estimate_observations GROUP BY run_id,request_id) obs "
        "ON obs.run_id=q.run_id AND obs.request_id=q.request_id "
        "ORDER BY q.run_id,q.request_id"
    )
    for row in request_rows:
        expected_request_id = digest(
            {
                "run_id": row["run_id"],
                "provider": row["provider"],
                "endpoint_id": row["endpoint_id"],
                "provider_symbol": row["provider_symbol"],
            }
        )
        if str(row["request_id"]) != expected_request_id:
            errors.append(f"request_id_mismatch:{row['request_id']}")
        try:
            cycle_started = _parse_utc(str(row["cycle_started_at_utc"]))
            request_started = _parse_utc(str(row["request_started_at_utc"]))
            response_received = _parse_utc(str(row["response_received_at_utc"]))
            cycle_completed = _parse_utc(str(row["cycle_completed_at_utc"]))
            if not cycle_started <= request_started <= response_received <= cycle_completed:
                errors.append(f"request_timestamp_order_invalid:{row['request_id']}")
        except ValueError:
            errors.append(f"request_timestamp_invalid:{row['request_id']}")
        status = str(row["status"])
        response_sha = str(row["response_sha256"])
        if status in CLEAN_REQUEST_STATUSES and not _is_sha256(response_sha):
            errors.append(f"clean_response_hash_invalid:{row['request_id']}")
        http_status = row["http_status"]
        if http_status is not None and not 100 <= int(http_status) <= 599:
            errors.append(f"request_http_status_invalid:{row['request_id']}")
        if int(row["elapsed_ms"]) < 0 or int(row["provider_row_count"]) < 0:
            errors.append(f"request_count_negative:{row['request_id']}")
        normalized_count = int(row["normalized_row_count"])
        if status == "AVAILABLE" and normalized_count <= 0:
            errors.append(f"available_request_without_rows:{row['request_id']}")
        if status != "AVAILABLE" and normalized_count:
            errors.append(f"nonavailable_request_with_rows:{row['request_id']}")
        if normalized_count != int(row["observation_count"]):
            errors.append(f"request_observation_count_mismatch:{row['request_id']}")
        if len(errors) >= 100:
            break

    run_counts = conn.execute(
        "SELECT cr.run_id,cr.request_count,cr.available_request_count,"
        "cr.empty_request_count,cr.error_request_count,cr.normalized_row_count,"
        "COUNT(q.request_id) AS actual_requests,"
        "COALESCE(SUM(CASE WHEN q.status='AVAILABLE' THEN 1 ELSE 0 END),0) AS actual_available,"
        "COALESCE(SUM(CASE WHEN q.status='EMPTY' THEN 1 ELSE 0 END),0) AS actual_empty,"
        "COALESCE(SUM(CASE WHEN q.status NOT IN ('AVAILABLE','EMPTY') THEN 1 ELSE 0 END),0) "
        "AS actual_errors,COALESCE(SUM(q.normalized_row_count),0) AS actual_normalized "
        "FROM capture_runs cr LEFT JOIN capture_requests q ON q.run_id=cr.run_id GROUP BY cr.run_id"
    )
    for row in run_counts:
        expected_counts = (
            int(row["request_count"]),
            int(row["available_request_count"]),
            int(row["empty_request_count"]),
            int(row["error_request_count"]),
            int(row["normalized_row_count"]),
        )
        actual_counts = (
            int(row["actual_requests"]),
            int(row["actual_available"]),
            int(row["actual_empty"]),
            int(row["actual_errors"]),
            int(row["actual_normalized"]),
        )
        if expected_counts != actual_counts:
            errors.append(f"run_request_count_mismatch:{row['run_id']}")
            if len(errors) >= 100:
                break
    live_runs = conn.execute(
        "SELECT run_id,run_digest FROM capture_runs WHERE status<>'MIGRATED' ORDER BY rowid"
    )
    for row in live_runs:
        expected = _stored_run_digest(conn, str(row["run_id"]))
        if str(row["run_digest"]) != expected:
            errors.append(f"run_digest_mismatch:{row['run_id']}")
    runs = conn.execute(
        "SELECT * FROM capture_runs WHERE status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') ORDER BY rowid"
    )
    previous = EMPTY_DIGEST
    for row in runs:
        if str(row["previous_pass_digest"]) != previous:
            errors.append(f"run_chain_break:{row['run_id']}")
        previous = str(row["run_digest"])
    errors.extend(_run_integrity_errors(conn))
    errors.extend(_universe_integrity_errors(conn))
    errors.extend(_version_integrity_errors(conn))
    errors.extend(_coverage_integrity_errors(conn))
    errors.extend(_change_integrity_errors(conn))
    errors.extend(_artifact_integrity_errors(conn))
    quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    if quick_check != ["ok"]:
        errors.extend(f"sqlite_quick_check:{item}" for item in quick_check)
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    errors.extend(f"foreign_key_violation:{row[0]}:{row[1]}:{row[2]}" for row in foreign_keys)
    missing = conn.execute(
        "SELECT COUNT(*) FROM estimate_observations o "
        "LEFT JOIN estimate_versions v ON v.version_id=o.version_id WHERE v.version_id IS NULL"
    ).fetchone()[0]
    if int(missing):
        errors.append(f"missing_versions:{missing}")
    invalid_effective = conn.execute(
        "SELECT COUNT(*) FROM estimate_observations WHERE effective_from_utc<available_at_utc"
    ).fetchone()[0]
    if int(invalid_effective):
        errors.append(f"effective_before_available:{invalid_effective}")
    failed_change_count = conn.execute(
        "SELECT COUNT(*) FROM estimate_changes c "
        "JOIN estimate_observations o ON o.observation_id=c.new_observation_id "
        "JOIN capture_runs cr ON cr.run_id=o.run_id WHERE cr.status='FAIL'"
    ).fetchone()[0]
    if int(failed_change_count):
        errors.append(f"failed_capture_changes:{failed_change_count}")
    # The production store contains millions of observations. Iterate cursors
    # directly so exhaustive integrity validation remains memory-bounded.
    observations = conn.execute(
        "SELECT observation_id,run_id,request_id,version_id,observed_at_utc,"
        "available_at_utc,effective_trading_date,effective_from_utc,"
        "prior_observation_id,observation_digest FROM estimate_observations"
    )
    for row in observations:
        observation_id = str(row["observation_id"])
        expected_id = digest(
            {
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "version_id": row["version_id"],
            }
        )
        if observation_id != expected_id:
            errors.append(f"observation_id_mismatch:{observation_id}")
        expected = digest(
            {
                "observation_id": observation_id,
                "version_id": row["version_id"],
                "available_at_utc": row["available_at_utc"],
                "effective_trading_date": row["effective_trading_date"],
                "prior_observation_id": row["prior_observation_id"] or "",
            }
        )
        if str(row["observation_digest"]) != expected:
            errors.append(f"observation_digest_mismatch:{observation_id}")
        try:
            _parse_utc(str(row["observed_at_utc"]))
            _parse_utc(str(row["available_at_utc"]))
            _parse_utc(str(row["effective_from_utc"]))
            date.fromisoformat(str(row["effective_trading_date"]))
        except ValueError:
            errors.append(f"observation_date_invalid:{observation_id}")
    alignment = conn.execute(
        "SELECT o.observation_id FROM estimate_observations o "
        "LEFT JOIN capture_requests q ON q.request_id=o.request_id "
        "LEFT JOIN estimate_versions v ON v.version_id=o.version_id "
        "WHERE q.request_id IS NULL OR v.version_id IS NULL OR o.run_id<>q.run_id "
        "OR q.instrument_id<>v.instrument_id OR q.ticker<>v.ticker "
        "OR q.provider<>v.provider OR q.provider_symbol<>v.provider_symbol "
        "OR q.endpoint_id<>v.endpoint_id OR o.observed_at_utc<>o.available_at_utc "
        "OR o.available_at_utc<>q.response_received_at_utc LIMIT 100"
    ).fetchall()
    errors.extend(f"observation_request_version_mismatch:{row['observation_id']}" for row in alignment)
    initial_unchanged = conn.execute(
        "SELECT observation_id FROM estimate_observations "
        "WHERE prior_observation_id IS NULL AND unchanged_from_prior<>0 LIMIT 100"
    ).fetchall()
    errors.extend(f"observation_initial_marked_unchanged:{row['observation_id']}" for row in initial_unchanged)
    priors = conn.execute(
        "SELECT o.observation_id,o.version_id,o.available_at_utc,"
        "o.unchanged_from_prior,p.version_id AS prior_version,"
        "p.available_at_utc AS prior_available,"
        "v.natural_key_hash,pv.natural_key_hash AS prior_natural "
        "FROM estimate_observations o "
        "LEFT JOIN estimate_observations p ON p.observation_id=o.prior_observation_id "
        "LEFT JOIN estimate_versions v ON v.version_id=o.version_id "
        "LEFT JOIN estimate_versions pv ON pv.version_id=p.version_id "
        "WHERE o.prior_observation_id IS NOT NULL ORDER BY o.observation_id"
    )
    for row in priors:
        observation_id = str(row["observation_id"])
        if row["prior_version"] is None:
            errors.append(f"observation_prior_missing:{observation_id}")
            continue
        if row["prior_natural"] != row["natural_key_hash"]:
            errors.append(f"observation_prior_natural_mismatch:{observation_id}")
        if str(row["prior_available"]) > str(row["available_at_utc"]):
            errors.append(f"observation_prior_after_current:{observation_id}")
        expected_unchanged = int(row["prior_version"] == row["version_id"])
        if int(row["unchanged_from_prior"]) != expected_unchanged:
            errors.append(f"observation_unchanged_flag_mismatch:{observation_id}")
    return errors
