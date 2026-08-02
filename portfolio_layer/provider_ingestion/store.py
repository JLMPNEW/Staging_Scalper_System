"""SQLite contracts and PIT actionability rules for provider observations."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "provider_observation_store_v1"
EMPTY_DIGEST = "0" * 64
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
       'provider_observation_store_v1' AS entitlement_version,
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


@contextmanager
def writer_lock(path: Path, *, timeout_sec: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    token = uuid.uuid4().hex
    payload = json.dumps({"token": token, "pid": os.getpid(), "host": socket.gethostname()})
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for provider-store writer lock: {path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            path.unlink(missing_ok=True)


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
    universe_id = digest(
        {"source_run_as_of": source_run_as_of, "capture_phase": capture_phase, "members": normalized}
    )
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


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must be timezone-aware: {value}")
    return parsed.astimezone(timezone.utc)


def _calendar_session(calendar_name: str, value: date, *, direction: str) -> tuple[Any, Any]:
    import exchange_calendars as xcals  # type: ignore[import-untyped]
    import pandas as pd  # type: ignore[import-untyped]

    calendar = xcals.get_calendar(calendar_name)
    return calendar, calendar.date_to_session(pd.Timestamp(value), direction=direction)


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
    return int(float(value)) if integer else float(value)


def _version_payload(
    row: Mapping[str, Any], *, ident: str, provider_symbol: str
) -> dict[str, Any]:
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
        field: _optional_number(
            row.get(field), integer=(field == "analyst_count" or "revision_" in field)
        )
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
        "WHERE v.natural_key_hash=? ORDER BY o.available_at_utc DESC,o.observation_id DESC LIMIT 1",
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
    prior = conn.execute(
        f"SELECT {columns} FROM estimate_versions WHERE version_id=?", (prior_version_id,)
    ).fetchone()
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
    run_fields = {
        key: run[key]
        for key in run.keys()
        if key != "run_digest"
    }
    requests = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM capture_requests WHERE run_id=? "
            "ORDER BY provider,endpoint_id,provider_symbol,request_id",
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
    completed_at_utc: str,
    request: Mapping[str, Any],
    timezone_name: str,
    calendar_name: str,
    decision_cutoff_local: str,
) -> tuple[int, int]:
    provider = str(request["provider"])
    ticker = str(request["ticker"]).strip().upper()
    ident = ensure_instrument(conn, ticker=ticker, providers=(provider,))
    provider_symbol = str(request.get("provider_symbol", ticker)).strip().upper()
    request_id = digest(
        {
            "run_id": run_id,
            "provider": provider,
            "endpoint_id": request["endpoint_id"],
            "provider_symbol": provider_symbol,
        }
    )
    normalized_rows = list(request.get("normalized_rows", []))
    prepared_versions: dict[str, dict[str, Any]] = {}
    for raw in normalized_rows:
        version = _version_payload(raw, ident=ident, provider_symbol=provider_symbol)
        natural_key = str(version["natural_key_hash"])
        prior_in_response = prepared_versions.get(natural_key)
        if prior_in_response is not None:
            if (
                prior_in_response["normalized_content_sha256"]
                != version["normalized_content_sha256"]
            ):
                raise ValueError(
                    "Provider response contains conflicting versions for one estimate key: "
                    f"{provider}/{ticker}/{version['estimate_type']}/{version['fiscal_period_end']}"
                )
            continue
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
        str(request["endpoint_id"]),
        str(request["request_started_at_utc"]),
        str(request["response_received_at_utc"]),
        str(request["status"]),
        request.get("http_status"),
        int(request.get("elapsed_ms", 0)),
        int(request.get("provider_row_count", 0)),
        len(normalized_rows),
        str(request.get("response_sha256", "")),
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
        observation_id = digest(
            {"run_id": run_id, "request_id": request_id, "version_id": version_id}
        )
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
        if prior is None or is_unchanged:
            continue
        before = prior["estimate_average"]
        after = version["estimate_average"]
        changed_fields = _changed_fields(
            conn, prior_version_id=str(prior["version_id"]), version=version
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
        summary = coverage.setdefault(
            key, {"requests": 0, "available": 0, "errors": 0, "ticker": ticker}
        )
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
        "SELECT run_digest,status,metadata_json FROM capture_runs WHERE cycle_id=?", (cycle_id,)
    ).fetchone()
    if existing is not None:
        existing_metadata = json.loads(str(existing["metadata_json"] or "{}"))
        existing_input_digest = str(existing_metadata.get("input_request_digest", ""))
        if str(existing["status"]) != "MIGRATED" and existing_input_digest != incoming_request_digest:
            raise ValueError(
                f"Capture cycle {cycle_id!r} already exists with different request content"
            )
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
                completed_at_utc=completed_at_utc,
                request=request,
                timezone_name=timezone_name,
                calendar_name=calendar_name,
                decision_cutoff_local=decision_cutoff_local,
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
            conn.execute(
                "UPDATE capture_runs SET run_digest=? WHERE run_id=?", (run_digest, run_id)
            )
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
            "UPDATE artifact_dependencies SET status='superseded' "
            "WHERE artifact_path=? AND status='active'",
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


def artifact_dependency_errors(
    conn: sqlite3.Connection, *, artifact_path: str, artifact_sha256: str
) -> list[str]:
    rows = conn.execute(
        "SELECT d.observation_id,o.observation_digest FROM artifact_dependencies d "
        "LEFT JOIN estimate_observations o ON o.observation_id=d.observation_id "
        "WHERE d.artifact_path=? AND d.artifact_sha256=? AND d.status='active'",
        (artifact_path, artifact_sha256),
    ).fetchall()
    if not rows:
        return ["no_active_dependencies"]
    return [
        f"missing_observation:{row['observation_id']}"
        for row in rows
        if not row["observation_digest"]
    ]


def verify_store(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    live_runs = conn.execute(
        "SELECT run_id,run_digest FROM capture_runs WHERE status<>'MIGRATED' ORDER BY rowid"
    ).fetchall()
    for row in live_runs:
        expected = _stored_run_digest(conn, str(row["run_id"]))
        if str(row["run_digest"]) != expected:
            errors.append(f"run_digest_mismatch:{row['run_id']}")
    runs = conn.execute(
        "SELECT * FROM capture_runs WHERE status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') "
        "ORDER BY rowid"
    ).fetchall()
    previous = EMPTY_DIGEST
    for row in runs:
        if str(row["previous_pass_digest"]) != previous:
            errors.append(f"run_chain_break:{row['run_id']}")
        previous = str(row["run_digest"])
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
    observations = conn.execute(
        "SELECT observation_id,version_id,available_at_utc,effective_trading_date,"
        "prior_observation_id,observation_digest FROM estimate_observations"
    ).fetchall()
    for row in observations:
        expected = digest(
            {
                "observation_id": row["observation_id"],
                "version_id": row["version_id"],
                "available_at_utc": row["available_at_utc"],
                "effective_trading_date": row["effective_trading_date"],
                "prior_observation_id": row["prior_observation_id"] or "",
            }
        )
        if str(row["observation_digest"]) != expected:
            errors.append(f"observation_digest_mismatch:{row['observation_id']}")
            if len(errors) >= 100:
                break
    return errors
