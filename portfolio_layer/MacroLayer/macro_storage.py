#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from macro_raw_config import parse_boolish, utc_now_iso
from macro_registry import MetricSpec
from macro_types import FetchResult, ObservationRecord, SourceArtifact

logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS macro_metric_registry (
    registry_key             TEXT PRIMARY KEY,
    metric_key               TEXT NOT NULL,
    regime_block             TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    source_dataset           TEXT,
    source_series_id         TEXT,
    ref_area                 TEXT NOT NULL,
    frequency                TEXT NOT NULL,
    seasonal_adjustment      TEXT,
    units                    TEXT,
    vintage_policy           TEXT NOT NULL,
    update_cadence           TEXT NOT NULL,
    history_start_date       TEXT,
    revision_window_days     INTEGER NOT NULL DEFAULT 0,
    source_priority          INTEGER NOT NULL DEFAULT 1,
    worker_hint              INTEGER NOT NULL DEFAULT 1,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    source_params_json       TEXT,
    notes                    TEXT,
    created_at_utc           TEXT NOT NULL,
    updated_at_utc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_observation_raw (
    observation_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key               TEXT NOT NULL UNIQUE,
    registry_key             TEXT NOT NULL,
    metric_key               TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    source_dataset           TEXT,
    source_series_id         TEXT,
    ref_area                 TEXT,
    frequency                TEXT,
    seasonal_adjustment      TEXT,
    units                    TEXT,
    observation_period       TEXT NOT NULL,
    observation_date         TEXT,
    release_date             TEXT,
    vintage_date             TEXT,
    value                    REAL NOT NULL,
    source_last_updated      TEXT,
    retrieved_at             TEXT NOT NULL,
    revision_flag            INTEGER NOT NULL DEFAULT 0,
    notes_hash               TEXT,
    ingest_run_id            TEXT,
    FOREIGN KEY (registry_key) REFERENCES macro_metric_registry(registry_key)
);

CREATE TABLE IF NOT EXISTS macro_sync_state (
    registry_key             TEXT PRIMARY KEY,
    metric_key               TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    last_observation_date    TEXT,
    last_release_date        TEXT,
    last_vintage_date        TEXT,
    last_source_last_updated TEXT,
    last_success_at_utc      TEXT,
    last_row_count           INTEGER NOT NULL DEFAULT 0,
    last_error_text          TEXT,
    updated_at_utc           TEXT NOT NULL,
    FOREIGN KEY (registry_key) REFERENCES macro_metric_registry(registry_key)
);

CREATE TABLE IF NOT EXISTS macro_ingest_run (
    run_id                   TEXT PRIMARY KEY,
    mode                     TEXT NOT NULL,
    as_of_date               TEXT NOT NULL,
    source_filter            TEXT,
    dry_run                  INTEGER NOT NULL DEFAULT 0,
    started_at_utc           TEXT NOT NULL,
    completed_at_utc         TEXT,
    status                   TEXT NOT NULL DEFAULT 'running',
    task_count               INTEGER NOT NULL DEFAULT 0,
    source_count             INTEGER NOT NULL DEFAULT 0,
    rows_written             INTEGER NOT NULL DEFAULT 0,
    error_count              INTEGER NOT NULL DEFAULT 0,
    notes                    TEXT
);

CREATE TABLE IF NOT EXISTS macro_source_artifact (
    artifact_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT NOT NULL,
    registry_key             TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    request_url              TEXT NOT NULL,
    payload_hash             TEXT,
    http_status              INTEGER,
    fetched_at_utc           TEXT NOT NULL,
    row_count                INTEGER NOT NULL DEFAULT 0,
    error_text               TEXT,
    extra_json               TEXT,
    FOREIGN KEY (run_id) REFERENCES macro_ingest_run(run_id),
    FOREIGN KEY (registry_key) REFERENCES macro_metric_registry(registry_key)
);

CREATE TABLE IF NOT EXISTS macro_country_metadata (
    ticker                   TEXT PRIMARY KEY,
    country_name             TEXT,
    ref_area                 TEXT,
    region                   TEXT,
    market_class             TEXT,
    commodity_profile        TEXT,
    energy_profile           TEXT,
    dollar_sensitivity       TEXT,
    inflation_targeting_flag TEXT,
    baseline_ticker          TEXT,
    country_pack_scope       TEXT,
    country_class            TEXT,
    oecd_ref_area            TEXT,
    imf_ref_area             TEXT,
    country_pack_enabled     INTEGER NOT NULL DEFAULT 1,
    oecd_primary_flag        INTEGER NOT NULL DEFAULT 1,
    imf_fx_fallback_flag     INTEGER NOT NULL DEFAULT 1,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    fred_fx_usd_series_id    TEXT,
    fred_fx_usd_units        TEXT,
    fred_neer_series_id      TEXT,
    fred_reer_series_id      TEXT,
    notes                    TEXT,
    updated_at_utc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_release_calendar (
    metric_key               TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    release_family           TEXT,
    cadence_rule             TEXT,
    publication_lag_days     INTEGER,
    timezone                 TEXT,
    notes                    TEXT,
    updated_at_utc           TEXT NOT NULL,
    PRIMARY KEY (metric_key, source_name)
);

CREATE TABLE IF NOT EXISTS macro_qa_run (
    qa_run_id                TEXT PRIMARY KEY,
    ingest_run_id            TEXT,
    as_of_date               TEXT NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'running',
    metric_count             INTEGER NOT NULL DEFAULT 0,
    issue_count              INTEGER NOT NULL DEFAULT 0,
    error_count              INTEGER NOT NULL DEFAULT 0,
    warning_count            INTEGER NOT NULL DEFAULT 0,
    started_at_utc           TEXT NOT NULL,
    completed_at_utc         TEXT,
    notes                    TEXT,
    FOREIGN KEY (ingest_run_id) REFERENCES macro_ingest_run(run_id)
);

CREATE TABLE IF NOT EXISTS macro_qa_issue (
    issue_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    qa_run_id                TEXT NOT NULL,
    severity                 TEXT NOT NULL,
    issue_type               TEXT NOT NULL,
    registry_key             TEXT,
    metric_key               TEXT,
    ref_area                 TEXT,
    source_name              TEXT,
    issue_count              INTEGER NOT NULL DEFAULT 1,
    details_json             TEXT,
    created_at_utc           TEXT NOT NULL,
    FOREIGN KEY (qa_run_id) REFERENCES macro_qa_run(qa_run_id)
);

CREATE TABLE IF NOT EXISTS macro_metric_span_summary (
    qa_run_id                TEXT NOT NULL,
    ingest_run_id            TEXT,
    registry_key             TEXT NOT NULL,
    metric_key               TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    ref_area                 TEXT,
    frequency                TEXT,
    vintage_policy           TEXT,
    observation_count        INTEGER NOT NULL DEFAULT 0,
    min_observation_date     TEXT,
    max_observation_date     TEXT,
    min_release_date         TEXT,
    max_release_date         TEXT,
    min_vintage_date         TEXT,
    max_vintage_date         TEXT,
    distinct_release_count   INTEGER NOT NULL DEFAULT 0,
    distinct_vintage_count   INTEGER NOT NULL DEFAULT 0,
    updated_at_utc           TEXT NOT NULL,
    PRIMARY KEY (qa_run_id, registry_key),
    FOREIGN KEY (qa_run_id) REFERENCES macro_qa_run(qa_run_id),
    FOREIGN KEY (registry_key) REFERENCES macro_metric_registry(registry_key)
);

CREATE TABLE IF NOT EXISTS macro_metric_freshness_summary (
    qa_run_id                TEXT NOT NULL,
    ingest_run_id            TEXT,
    registry_key             TEXT NOT NULL,
    metric_key               TEXT NOT NULL,
    source_name              TEXT NOT NULL,
    ref_area                 TEXT,
    frequency                TEXT,
    as_of_date               TEXT NOT NULL,
    latest_observation_date  TEXT,
    freshness_days           INTEGER,
    max_staleness_days       INTEGER,
    carry_forward_allowed    INTEGER NOT NULL DEFAULT 1,
    is_stale                 INTEGER NOT NULL DEFAULT 0,
    source_quality_weight    REAL,
    updated_at_utc           TEXT NOT NULL,
    PRIMARY KEY (qa_run_id, registry_key),
    FOREIGN KEY (qa_run_id) REFERENCES macro_qa_run(qa_run_id),
    FOREIGN KEY (registry_key) REFERENCES macro_metric_registry(registry_key)
);

CREATE TABLE IF NOT EXISTS macro_country_coverage_summary (
    qa_run_id                     TEXT NOT NULL,
    ingest_run_id                TEXT,
    ticker                       TEXT NOT NULL,
    ref_area                     TEXT NOT NULL,
    country_class                TEXT,
    expected_metric_count        INTEGER NOT NULL DEFAULT 0,
    available_metric_count       INTEGER NOT NULL DEFAULT 0,
    required_metric_count        INTEGER NOT NULL DEFAULT 0,
    available_required_count     INTEGER NOT NULL DEFAULT 0,
    stale_metric_count           INTEGER NOT NULL DEFAULT 0,
    coverage_ratio               REAL,
    required_coverage_ratio      REAL,
    missing_required_metrics_json TEXT,
    updated_at_utc               TEXT NOT NULL,
    PRIMARY KEY (qa_run_id, ref_area),
    FOREIGN KEY (qa_run_id) REFERENCES macro_qa_run(qa_run_id)
);

CREATE INDEX IF NOT EXISTS idx_macro_obs_metric_date
    ON macro_observation_raw(metric_key, observation_date);

CREATE INDEX IF NOT EXISTS idx_macro_obs_registry_vintage
    ON macro_observation_raw(registry_key, vintage_date, observation_date);

CREATE INDEX IF NOT EXISTS idx_macro_obs_source_metric
    ON macro_observation_raw(source_name, metric_key, observation_date);

CREATE INDEX IF NOT EXISTS idx_macro_obs_metric_release_vintage_obs
    ON macro_observation_raw(metric_key, release_date, vintage_date, observation_date);

CREATE INDEX IF NOT EXISTS idx_macro_obs_ref_metric_date
    ON macro_observation_raw(ref_area, metric_key, observation_date);

CREATE INDEX IF NOT EXISTS idx_macro_obs_metric_period_obs_vintage
    ON macro_observation_raw(metric_key, observation_period, observation_date, vintage_date);

CREATE INDEX IF NOT EXISTS idx_macro_sync_source
    ON macro_sync_state(source_name, metric_key);

CREATE INDEX IF NOT EXISTS idx_macro_qa_issue_run_severity
    ON macro_qa_issue(qa_run_id, severity, issue_type);

CREATE INDEX IF NOT EXISTS idx_macro_metric_span_run_metric
    ON macro_metric_span_summary(qa_run_id, metric_key, ref_area);

CREATE INDEX IF NOT EXISTS idx_macro_metric_freshness_run_stale
    ON macro_metric_freshness_summary(qa_run_id, is_stale, frequency);

CREATE INDEX IF NOT EXISTS idx_macro_country_coverage_run_ref
    ON macro_country_coverage_summary(qa_run_id, ref_area, country_class);
"""


LATEST_CURRENT_VIEW = """
CREATE VIEW IF NOT EXISTS macro_observation_latest_current_v AS
WITH ranked AS (
    SELECT
        o.*,
        r.source_priority,
        ROW_NUMBER() OVER (
            PARTITION BY o.metric_key, o.registry_key
            ORDER BY
                COALESCE(o.observation_date, o.observation_period) DESC,
                COALESCE(o.vintage_date, '') DESC,
                o.retrieved_at DESC
        ) AS rn
    FROM macro_observation_raw o
    JOIN macro_metric_registry r
      ON r.registry_key = o.registry_key
)
SELECT *
FROM ranked
WHERE rn = 1;
"""


PREFERRED_LATEST_VIEW = """
CREATE VIEW IF NOT EXISTS macro_observation_preferred_latest_v AS
WITH latest_per_registry AS (
    SELECT
        o.*,
        r.source_priority,
        ROW_NUMBER() OVER (
            PARTITION BY o.registry_key
            ORDER BY
                COALESCE(o.observation_date, o.observation_period) DESC,
                COALESCE(o.vintage_date, '') DESC,
                o.retrieved_at DESC
        ) AS registry_rn
    FROM macro_observation_raw o
    JOIN macro_metric_registry r
      ON r.registry_key = o.registry_key
    WHERE r.enabled = 1
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY metric_key
            ORDER BY source_priority ASC, COALESCE(observation_date, observation_period) DESC
        ) AS metric_rn
    FROM latest_per_registry
    WHERE registry_rn = 1
)
SELECT *
FROM ranked
WHERE metric_rn = 1;
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    _ensure_country_metadata_columns(conn)
    conn.executescript(LATEST_CURRENT_VIEW)
    conn.executescript(PREFERRED_LATEST_VIEW)
    conn.commit()


def _ensure_country_metadata_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(macro_country_metadata)").fetchall()
    }
    desired = {
        "country_pack_scope": "TEXT",
        "country_class": "TEXT",
        "oecd_ref_area": "TEXT",
        "imf_ref_area": "TEXT",
        "country_pack_enabled": "INTEGER NOT NULL DEFAULT 1",
        "oecd_primary_flag": "INTEGER NOT NULL DEFAULT 1",
        "imf_fx_fallback_flag": "INTEGER NOT NULL DEFAULT 1",
        "fred_fx_usd_series_id": "TEXT",
        "fred_fx_usd_units": "TEXT",
        "fred_neer_series_id": "TEXT",
        "fred_reer_series_id": "TEXT",
    }
    altered = False
    for col_name, decl in desired.items():
        if col_name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE macro_country_metadata ADD COLUMN {col_name} {decl}")
            altered = True
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    if altered:
        conn.commit()


def upsert_metric_registry(conn: sqlite3.Connection, specs: Iterable[MetricSpec]) -> None:
    now = utc_now_iso()
    rows = []
    for spec in specs:
        rows.append(
            (
                spec.registry_key,
                spec.metric_key,
                spec.regime_block,
                spec.source_name,
                spec.source_dataset,
                spec.source_series_id,
                spec.ref_area,
                spec.frequency,
                spec.seasonal_adjustment,
                spec.units,
                spec.vintage_policy,
                spec.update_cadence,
                spec.history_start_date.isoformat() if spec.history_start_date else None,
                spec.revision_window_days,
                spec.source_priority,
                spec.worker_hint,
                1 if spec.enabled else 0,
                json.dumps(spec.source_params, separators=(",", ":"), sort_keys=True),
                spec.notes,
                now,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO macro_metric_registry (
            registry_key, metric_key, regime_block, source_name, source_dataset, source_series_id,
            ref_area, frequency, seasonal_adjustment, units, vintage_policy, update_cadence,
            history_start_date, revision_window_days, source_priority, worker_hint, enabled,
            source_params_json, notes, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(registry_key) DO UPDATE SET
            metric_key=excluded.metric_key,
            regime_block=excluded.regime_block,
            source_name=excluded.source_name,
            source_dataset=excluded.source_dataset,
            source_series_id=excluded.source_series_id,
            ref_area=excluded.ref_area,
            frequency=excluded.frequency,
            seasonal_adjustment=excluded.seasonal_adjustment,
            units=excluded.units,
            vintage_policy=excluded.vintage_policy,
            update_cadence=excluded.update_cadence,
            history_start_date=excluded.history_start_date,
            revision_window_days=excluded.revision_window_days,
            source_priority=excluded.source_priority,
            worker_hint=excluded.worker_hint,
            enabled=excluded.enabled,
            source_params_json=excluded.source_params_json,
            notes=excluded.notes,
            updated_at_utc=excluded.updated_at_utc
        """,
        rows,
    )
    conn.commit()


def start_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    mode: str,
    as_of_date: str,
    source_filter: str | None,
    dry_run: bool,
    task_count: int,
    source_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO macro_ingest_run (
            run_id, mode, as_of_date, source_filter, dry_run, started_at_utc, task_count, source_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, mode, as_of_date, source_filter, 1 if dry_run else 0, utc_now_iso(), task_count, source_count),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    rows_written: int,
    error_count: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE macro_ingest_run
        SET completed_at_utc = ?, status = ?, rows_written = ?, error_count = ?, notes = ?
        WHERE run_id = ?
        """,
        (utc_now_iso(), status, rows_written, error_count, notes, run_id),
    )
    conn.commit()


def load_sync_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT registry_key, metric_key, source_name, last_observation_date, last_release_date,
               last_vintage_date, last_source_last_updated, last_success_at_utc,
               last_row_count, last_error_text, updated_at_utc
        FROM macro_sync_state
        """
    ).fetchall()
    state: dict[str, dict[str, Any]] = {}
    for row in rows:
        state[str(row[0])] = {
            "registry_key": row[0],
            "metric_key": row[1],
            "source_name": row[2],
            "last_observation_date": row[3],
            "last_release_date": row[4],
            "last_vintage_date": row[5],
            "last_source_last_updated": row[6],
            "last_success_at_utc": row[7],
            "last_row_count": row[8],
            "last_error_text": row[9],
            "updated_at_utc": row[10],
        }
    return state


def write_fetch_result(conn: sqlite3.Connection, run_id: str, result: FetchResult) -> int:
    rows_written = 0
    if result.error_text and result.observations:
        logger.warning(
            "write_fetch_result: result has both error_text and observations for %s; writing both",
            result.spec.registry_key,
        )
    if result.artifacts:
        _insert_artifacts(conn, run_id=run_id, artifacts=result.artifacts)
    if result.observations:
        rows_written = _upsert_observations(
            conn,
            run_id=run_id,
            registry_key=result.spec.registry_key,
            observations=result.observations,
        )
    _upsert_sync_state(conn, result=result)
    conn.commit()
    return rows_written


def _insert_artifacts(conn: sqlite3.Connection, run_id: str, artifacts: Iterable[SourceArtifact]) -> None:
    rows = []
    for artifact in artifacts:
        rows.append(
            (
                run_id,
                artifact.registry_key,
                artifact.source_name,
                artifact.request_url,
                artifact.payload_hash,
                artifact.http_status,
                artifact.fetched_at,
                artifact.row_count,
                artifact.error_text,
                json.dumps(artifact.extra_json, separators=(",", ":"), sort_keys=True) if artifact.extra_json else None,
            )
        )
    conn.executemany(
        """
        INSERT INTO macro_source_artifact (
            run_id, registry_key, source_name, request_url, payload_hash, http_status,
            fetched_at_utc, row_count, error_text, extra_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _make_dedupe_key(registry_key: str, obs: ObservationRecord) -> str:
    text = "|".join(
        [
            registry_key,
            obs.metric_key,
            obs.source_name,
            obs.source_series_id or "",
            obs.observation_period,
            obs.release_date or "",
            obs.vintage_date or "",
        ]
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _upsert_observations(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    registry_key: str,
    observations: Iterable[ObservationRecord],
) -> int:
    payload = []
    count = 0
    for obs in observations:
        payload.append(
            (
                _make_dedupe_key(registry_key, obs),
                registry_key,
                obs.metric_key,
                obs.source_name,
                obs.source_dataset,
                obs.source_series_id,
                obs.ref_area,
                obs.frequency,
                obs.seasonal_adjustment,
                obs.units,
                obs.observation_period,
                obs.observation_date,
                obs.release_date,
                obs.vintage_date,
                obs.value,
                obs.source_last_updated,
                obs.retrieved_at,
                obs.revision_flag,
                obs.notes_hash,
                run_id,
            )
        )
        count += 1
    conn.executemany(
        """
        INSERT INTO macro_observation_raw (
            dedupe_key, registry_key, metric_key, source_name, source_dataset, source_series_id,
            ref_area, frequency, seasonal_adjustment, units, observation_period, observation_date,
            release_date, vintage_date, value, source_last_updated, retrieved_at, revision_flag,
            notes_hash, ingest_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dedupe_key) DO UPDATE SET
            value=excluded.value,
            source_last_updated=excluded.source_last_updated,
            retrieved_at=excluded.retrieved_at,
            revision_flag=CASE
                WHEN macro_observation_raw.value IS NOT excluded.value THEN 1
                ELSE macro_observation_raw.revision_flag
            END,
            notes_hash=excluded.notes_hash,
            ingest_run_id=excluded.ingest_run_id
        """,
        payload,
    )
    return count


def _upsert_sync_state(conn: sqlite3.Connection, *, result: FetchResult) -> None:
    max_obs = _max_field(result.observations, "observation_date")
    max_release = _max_field(result.observations, "release_date")
    max_vintage = _max_field(result.observations, "vintage_date")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO macro_sync_state (
            registry_key, metric_key, source_name, last_observation_date, last_release_date,
            last_vintage_date, last_source_last_updated, last_success_at_utc, last_row_count,
            last_error_text, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(registry_key) DO UPDATE SET
            metric_key=excluded.metric_key,
            source_name=excluded.source_name,
            last_observation_date=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_observation_date
                ELSE excluded.last_observation_date
            END,
            last_release_date=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_release_date
                ELSE excluded.last_release_date
            END,
            last_vintage_date=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_vintage_date
                ELSE excluded.last_vintage_date
            END,
            last_source_last_updated=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_source_last_updated
                ELSE excluded.last_source_last_updated
            END,
            last_success_at_utc=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_success_at_utc
                ELSE excluded.last_success_at_utc
            END,
            last_row_count=CASE
                WHEN excluded.last_error_text IS NOT NULL THEN macro_sync_state.last_row_count
                ELSE excluded.last_row_count
            END,
            last_error_text=excluded.last_error_text,
            updated_at_utc=excluded.updated_at_utc
        """,
        (
            result.spec.registry_key,
            result.spec.metric_key,
            result.spec.source_name,
            max_obs,
            max_release,
            max_vintage,
            result.source_last_updated,
            now if result.error_text is None else None,
            result.row_count,
            result.error_text,
            now,
        ),
    )


def _max_field(observations: Iterable[ObservationRecord], attr: str) -> str | None:
    values = [getattr(obs, attr) for obs in observations if getattr(obs, attr)]
    if not values:
        return None
    return max(str(value) for value in values)


def seed_country_metadata(conn: sqlite3.Connection, csv_path: Path | None) -> None:
    if csv_path is None or not csv_path.exists():
        return
    now = utc_now_iso()
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ticker = str(row.get("ticker", "") or "").strip().upper()
            if not ticker:
                continue
            rows.append(
                (
                    ticker,
                    str(row.get("country_name", "") or "").strip() or None,
                    str(row.get("ref_area", "") or "").strip() or None,
                    str(row.get("region", "") or "").strip() or None,
                    str(row.get("market_class", "") or "").strip() or None,
                    str(row.get("commodity_profile", "") or "").strip() or None,
                    str(row.get("energy_profile", "") or "").strip() or None,
                    str(row.get("dollar_sensitivity", "") or "").strip() or None,
                    str(row.get("inflation_targeting_flag", "") or "").strip() or None,
                    str(row.get("baseline_ticker", "") or "").strip() or None,
                    str(row.get("country_pack_scope", "") or "").strip() or None,
                    str(row.get("country_class", "") or "").strip() or None,
                    str(row.get("oecd_ref_area", "") or "").strip() or None,
                    str(row.get("imf_ref_area", "") or "").strip() or None,
                    1 if parse_boolish(row.get("country_pack_enabled"), default=True) else 0,
                    1 if parse_boolish(row.get("oecd_primary_flag"), default=True) else 0,
                    1 if parse_boolish(row.get("imf_fx_fallback_flag"), default=True) else 0,
                    1 if parse_boolish(row.get("enabled"), default=True) else 0,
                    str(row.get("fred_fx_usd_series_id", "") or "").strip() or None,
                    str(row.get("fred_fx_usd_units", "") or "").strip() or None,
                    str(row.get("fred_neer_series_id", "") or "").strip() or None,
                    str(row.get("fred_reer_series_id", "") or "").strip() or None,
                    str(row.get("notes", "") or "").strip() or None,
                    now,
                )
            )
    conn.executemany(
        """
        INSERT INTO macro_country_metadata (
            ticker, country_name, ref_area, region, market_class, commodity_profile,
            energy_profile, dollar_sensitivity, inflation_targeting_flag, baseline_ticker,
            country_pack_scope, country_class, oecd_ref_area, imf_ref_area, country_pack_enabled,
            oecd_primary_flag, imf_fx_fallback_flag, enabled, fred_fx_usd_series_id, fred_fx_usd_units,
            fred_neer_series_id, fred_reer_series_id, notes, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            country_name=excluded.country_name,
            ref_area=excluded.ref_area,
            region=excluded.region,
            market_class=excluded.market_class,
            commodity_profile=excluded.commodity_profile,
            energy_profile=excluded.energy_profile,
            dollar_sensitivity=excluded.dollar_sensitivity,
            inflation_targeting_flag=excluded.inflation_targeting_flag,
            baseline_ticker=excluded.baseline_ticker,
            country_pack_scope=excluded.country_pack_scope,
            country_class=excluded.country_class,
            oecd_ref_area=excluded.oecd_ref_area,
            imf_ref_area=excluded.imf_ref_area,
            country_pack_enabled=excluded.country_pack_enabled,
            oecd_primary_flag=excluded.oecd_primary_flag,
            imf_fx_fallback_flag=excluded.imf_fx_fallback_flag,
            enabled=excluded.enabled,
            fred_fx_usd_series_id=excluded.fred_fx_usd_series_id,
            fred_fx_usd_units=excluded.fred_fx_usd_units,
            fred_neer_series_id=excluded.fred_neer_series_id,
            fred_reer_series_id=excluded.fred_reer_series_id,
            notes=excluded.notes,
            updated_at_utc=excluded.updated_at_utc
        """,
        rows,
    )
    conn.commit()


def seed_release_calendar(conn: sqlite3.Connection, csv_path: Path | None) -> None:
    if csv_path is None or not csv_path.exists():
        return
    now = utc_now_iso()
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            metric_key = str(row.get("metric_key", "") or "").strip()
            source_name = str(row.get("source_name", "") or "").strip()
            if not metric_key or not source_name:
                continue
            lag_raw = str(row.get("publication_lag_days", "") or "").strip()
            try:
                lag_value = int(lag_raw) if lag_raw else None
            except ValueError:
                lag_value = None
            rows.append(
                (
                    metric_key,
                    source_name,
                    str(row.get("release_family", "") or "").strip() or None,
                    str(row.get("cadence_rule", "") or "").strip() or None,
                    lag_value,
                    str(row.get("timezone", "") or "").strip() or None,
                    str(row.get("notes", "") or "").strip() or None,
                    now,
                )
            )
    conn.executemany(
        """
        INSERT INTO macro_release_calendar (
            metric_key, source_name, release_family, cadence_rule, publication_lag_days,
            timezone, notes, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(metric_key, source_name) DO UPDATE SET
            release_family=excluded.release_family,
            cadence_rule=excluded.cadence_rule,
            publication_lag_days=excluded.publication_lag_days,
            timezone=excluded.timezone,
            notes=excluded.notes,
            updated_at_utc=excluded.updated_at_utc
        """,
        rows,
    )
    conn.commit()
