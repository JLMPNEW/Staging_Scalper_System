from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


UNIVERSE_FIELDS = [
    "run_as_of",
    "ticker",
    "tier",
    "priority_rank",
    "is_holding",
    "is_target",
    "is_pending_order",
    "is_scored",
    "investable_eligible",
    "source_pipeline",
    "sector",
    "industry",
    "rating",
    "final_score",
    "score_confidence",
    "target_weight",
    "net_shares",
    "score_source_asof_date",
    "updated_at_utc",
]


def monitor_output_subdir(config: dict[str, Any]) -> str:
    monitor = config.get("expectations_monitor", {})
    if not isinstance(monitor, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    value = str(monitor.get("output_subdir", "expectations_monitor")).strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("expectations_monitor.output_subdir must be a safe relative path")
    return value


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS monitor_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_as_of TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS monitor_source_artifacts (
    run_as_of TEXT NOT NULL,
    source_role TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    acceptance TEXT NOT NULL,
    PRIMARY KEY (run_as_of, source_role)
);

CREATE TABLE IF NOT EXISTS monitor_universe (
    run_as_of TEXT NOT NULL,
    ticker TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('tier0', 'tier1', 'tier2')),
    priority_rank INTEGER NOT NULL CHECK (priority_rank IN (0, 1, 2)),
    is_holding INTEGER NOT NULL CHECK (is_holding IN (0, 1)),
    is_target INTEGER NOT NULL CHECK (is_target IN (0, 1)),
    is_pending_order INTEGER NOT NULL CHECK (is_pending_order IN (0, 1)),
    is_scored INTEGER NOT NULL CHECK (is_scored IN (0, 1)),
    investable_eligible INTEGER NOT NULL CHECK (investable_eligible IN (0, 1)),
    source_pipeline TEXT NOT NULL DEFAULT '',
    sector TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL DEFAULT '',
    final_score REAL,
    score_confidence REAL,
    target_weight REAL NOT NULL DEFAULT 0.0,
    net_shares REAL NOT NULL DEFAULT 0.0,
    score_source_asof_date TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (run_as_of, ticker)
);

CREATE INDEX IF NOT EXISTS ix_monitor_universe_tier
ON monitor_universe(run_as_of, tier, ticker);

CREATE TABLE IF NOT EXISTS provider_snapshot_runs (
    snapshot_run_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    retrieval_cycle TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL,
    requested_count INTEGER NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    entitlement_sha256 TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    UNIQUE (provider, endpoint_id, retrieval_cycle)
);

CREATE TABLE IF NOT EXISTS provider_estimate_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_run_id TEXT,
    provider TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,
    fiscal_period TEXT NOT NULL DEFAULT '',
    estimate_type TEXT NOT NULL,
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
    currency TEXT NOT NULL DEFAULT '',
    provider_published_at_utc TEXT NOT NULL DEFAULT '',
    fetched_at_utc TEXT NOT NULL,
    available_at_utc TEXT NOT NULL,
    retrieval_cycle TEXT NOT NULL,
    source_uid TEXT NOT NULL DEFAULT '',
    response_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    entitlement_version TEXT NOT NULL,
    retention_class TEXT NOT NULL CHECK (
        retention_class IN ('provisional_user_authorized', 'confirmed')
    ),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('available', 'missing', 'stale', 'invalid')
    ),
    FOREIGN KEY (snapshot_run_id) REFERENCES provider_snapshot_runs(snapshot_run_id),
    UNIQUE (
        provider, endpoint_id, ticker, fiscal_period_end,
        estimate_type, retrieval_cycle
    )
);

CREATE INDEX IF NOT EXISTS ix_provider_estimate_snapshot_lookup
ON provider_estimate_snapshots(provider, ticker, fetched_at_utc, estimate_type);

CREATE INDEX IF NOT EXISTS ix_provider_estimate_revision_history
ON provider_estimate_snapshots(
    provider, ticker, estimate_type, fiscal_period_end, available_at_utc
);

CREATE TABLE IF NOT EXISTS provider_snapshot_dependencies (
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'invalidated')),
    recorded_at_utc TEXT NOT NULL,
    invalidated_at_utc TEXT,
    invalidation_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artifact_path, artifact_sha256, snapshot_id)
);

CREATE INDEX IF NOT EXISTS ix_provider_snapshot_dependencies_snapshot
ON provider_snapshot_dependencies(snapshot_id, status);

CREATE TABLE IF NOT EXISTS provider_metric_basis_snapshots (
    basis_snapshot_id TEXT PRIMARY KEY,
    estimate_provider TEXT NOT NULL CHECK (estimate_provider IN ('alpha_vantage', 'fmp')),
    currency_source_provider TEXT NOT NULL CHECK (currency_source_provider IN ('fmp', 'manual')),
    endpoint_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('eps', 'revenue')),
    reporting_currency TEXT NOT NULL DEFAULT '',
    statement_period_end TEXT NOT NULL DEFAULT '',
    metric_definition TEXT NOT NULL,
    unit_scale TEXT NOT NULL,
    per_share_basis TEXT NOT NULL,
    currency_semantics_status TEXT NOT NULL,
    definition_semantics_status TEXT NOT NULL,
    comparison_eligible INTEGER NOT NULL CHECK (comparison_eligible IN (0, 1)),
    ineligibility_reasons TEXT NOT NULL DEFAULT '',
    fetched_at_utc TEXT NOT NULL,
    available_at_utc TEXT NOT NULL,
    retrieval_cycle TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    entitlement_version TEXT NOT NULL,
    retention_class TEXT NOT NULL CHECK (
        retention_class IN ('provisional_user_authorized', 'confirmed')
    ),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('available', 'missing', 'stale', 'invalid')
    ),
    UNIQUE (estimate_provider, ticker, metric, retrieval_cycle)
);

CREATE INDEX IF NOT EXISTS ix_provider_metric_basis_lookup
ON provider_metric_basis_snapshots(estimate_provider, ticker, metric, fetched_at_utc);

CREATE TABLE IF NOT EXISTS provider_fiscal_period_resolutions (
    resolution_id TEXT PRIMARY KEY,
    source_provider TEXT NOT NULL CHECK (source_provider = 'alpha_vantage'),
    endpoint_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,
    fiscal_period TEXT NOT NULL CHECK (fiscal_period = 'quarterly'),
    report_time TEXT NOT NULL DEFAULT '',
    resolution_status TEXT NOT NULL,
    resolution_eligible INTEGER NOT NULL CHECK (resolution_eligible IN (0, 1)),
    ineligibility_reasons TEXT NOT NULL DEFAULT '',
    fetched_at_utc TEXT NOT NULL,
    available_at_utc TEXT NOT NULL,
    retrieval_cycle TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    entitlement_version TEXT NOT NULL,
    retention_class TEXT NOT NULL CHECK (
        retention_class IN ('provisional_user_authorized', 'confirmed')
    ),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('available', 'missing', 'stale', 'invalid')
    ),
    UNIQUE (source_provider, ticker, report_date, fiscal_period_end, retrieval_cycle)
);

CREATE INDEX IF NOT EXISTS ix_provider_fiscal_period_resolution_lookup
ON provider_fiscal_period_resolutions(ticker, report_date, fiscal_period_end, available_at_utc);

CREATE TABLE IF NOT EXISTS provider_actual_outcomes_v2 (
    outcome_id TEXT PRIMARY KEY,
    row_sequence INTEGER NOT NULL UNIQUE CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider IN ('alpha_vantage', 'fmp')),
    endpoint_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL DEFAULT '',
    outcome_period_status TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('eps', 'revenue')),
    actual_value REAL NOT NULL,
    reporting_currency TEXT NOT NULL DEFAULT '',
    metric_basis_id TEXT NOT NULL DEFAULT '',
    metric_basis_status TEXT NOT NULL,
    provider_updated_at_raw TEXT NOT NULL DEFAULT '',
    provider_published_at_utc TEXT NOT NULL DEFAULT '',
    fetched_at_utc TEXT NOT NULL,
    available_at_utc TEXT NOT NULL,
    retrieval_cycle TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    entitlement_version TEXT NOT NULL,
    retention_class TEXT NOT NULL CHECK (
        retention_class IN ('provisional_user_authorized', 'confirmed')
    ),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('available', 'missing', 'stale', 'invalid')
    ),
    evaluation_eligible INTEGER NOT NULL CHECK (evaluation_eligible IN (0, 1)),
    ineligibility_reasons TEXT NOT NULL DEFAULT '',
    UNIQUE (provider, endpoint_id, ticker, report_date, metric, retrieval_cycle)
);

CREATE INDEX IF NOT EXISTS ix_provider_actual_outcome_v2_lookup
ON provider_actual_outcomes_v2(ticker, metric, report_date, fiscal_period_end, fetched_at_utc);

CREATE TABLE IF NOT EXISTS provider_forecast_outcome_links_v2 (
    link_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    linked_at_utc TEXT NOT NULL,
    forecast_available_at_utc TEXT NOT NULL,
    outcome_available_at_utc TEXT NOT NULL,
    forecast_lead_days REAL,
    evaluation_status TEXT NOT NULL,
    ineligibility_reasons TEXT NOT NULL DEFAULT '',
    error_value REAL,
    absolute_error REAL,
    normalized_absolute_error REAL,
    FOREIGN KEY (snapshot_id) REFERENCES provider_estimate_snapshots(snapshot_id),
    FOREIGN KEY (outcome_id) REFERENCES provider_actual_outcomes_v2(outcome_id),
    UNIQUE (snapshot_id, outcome_id)
);

CREATE TABLE IF NOT EXISTS provider_forecast_outcome_links_v3 (
    link_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    resolution_id TEXT NOT NULL,
    evaluation_cycle TEXT NOT NULL,
    basis_snapshot_id TEXT NOT NULL DEFAULT '',
    estimate_provider TEXT NOT NULL CHECK (estimate_provider IN ('alpha_vantage', 'fmp')),
    ticker TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('eps', 'revenue')),
    canonical_period TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,
    linked_at_utc TEXT NOT NULL,
    forecast_available_at_utc TEXT NOT NULL,
    outcome_available_at_utc TEXT NOT NULL,
    cutoff_policy TEXT NOT NULL,
    forecast_lead_days REAL NOT NULL,
    forecast_value REAL NOT NULL,
    actual_value REAL NOT NULL,
    evaluation_status TEXT NOT NULL CHECK (evaluation_status IN ('eligible', 'ineligible')),
    ineligibility_reasons TEXT NOT NULL DEFAULT '',
    error_value REAL,
    absolute_error REAL,
    normalized_absolute_error REAL,
    normalized_sha256 TEXT NOT NULL,
    UNIQUE (snapshot_id, outcome_id, resolution_id, evaluation_cycle)
);

CREATE INDEX IF NOT EXISTS ix_provider_forecast_outcome_link_v3_lookup
ON provider_forecast_outcome_links_v3(
    estimate_provider, ticker, metric, fiscal_period_end, report_date
);

CREATE TABLE IF NOT EXISTS provider_purge_events (
    purge_event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    requested_at_utc TEXT NOT NULL,
    executed_at_utc TEXT NOT NULL,
    reason TEXT NOT NULL,
    deleted_snapshot_count INTEGER NOT NULL,
    invalidated_dependency_count INTEGER NOT NULL,
    invalidated_artifacts_json TEXT NOT NULL
);
"""

SNAPSHOT_VALUE_FIELDS = [
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
]

BASIS_FIELDS = [
    'estimate_provider',
    'currency_source_provider',
    'endpoint_id',
    'ticker',
    'metric',
    'reporting_currency',
    'statement_period_end',
    'metric_definition',
    'unit_scale',
    'per_share_basis',
    'currency_semantics_status',
    'definition_semantics_status',
    'comparison_eligible',
    'ineligibility_reasons',
    'fetched_at_utc',
    'available_at_utc',
    'retrieval_cycle',
    'response_sha256',
    'entitlement_version',
    'retention_class',
    'coverage_status',
]

FISCAL_PERIOD_RESOLUTION_FIELDS = [
    'source_provider',
    'endpoint_id',
    'ticker',
    'report_date',
    'fiscal_period_end',
    'fiscal_period',
    'report_time',
    'resolution_status',
    'resolution_eligible',
    'ineligibility_reasons',
    'fetched_at_utc',
    'available_at_utc',
    'retrieval_cycle',
    'response_sha256',
    'entitlement_version',
    'retention_class',
    'coverage_status',
]

ACTUAL_OUTCOME_VALUE_FIELDS = [
    'provider',
    'endpoint_id',
    'ticker',
    'report_date',
    'fiscal_period_end',
    'outcome_period_status',
    'metric',
    'actual_value',
    'reporting_currency',
    'metric_basis_id',
    'metric_basis_status',
    'provider_updated_at_raw',
    'provider_published_at_utc',
    'fetched_at_utc',
    'available_at_utc',
    'retrieval_cycle',
    'response_sha256',
    'entitlement_version',
    'retention_class',
    'coverage_status',
    'evaluation_eligible',
    'ineligibility_reasons',
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _number(value: Any, *, default: float = 0.0) -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite numeric value: {value!r}")
    return parsed


def _flag(value: Any) -> int:
    text = str(value if value is not None else "").strip().casefold()
    if text in {"1", "1.0", "true", "yes", "y"}:
        return 1
    if text in {"", "0", "0.0", "false", "no", "n"}:
        return 0
    raise ValueError(f"Invalid binary flag: {value!r}")


def _optional_number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _number(value)


def _optional_integer(value: Any) -> int | None:
    parsed = _optional_number(value)
    if parsed is None:
        return None
    if not parsed.is_integer():
        raise ValueError(f"Expected integer-valued number: {value!r}")
    return int(parsed)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: Any, *, label: str) -> str:
    text = str(value if value is not None else "").strip().casefold()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return text


def _ticker(value: Any) -> str:
    ticker = str(value if value is not None else "").strip().upper()
    if not ticker or ticker == "CASH" or any(char.isspace() for char in ticker):
        return ""
    return ticker


def build_universe_rows(
    *,
    run_as_of: str,
    score_rows: Iterable[dict[str, Any]],
    target_rows: Iterable[dict[str, Any]],
    holding_rows: Iterable[dict[str, Any]],
    updated_at_utc: str,
    pending_order_tickers: Iterable[str] = (),
) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for row in score_rows:
        ticker = _ticker(row.get("ticker"))
        if not ticker:
            continue
        if ticker in scores:
            raise ValueError(f"Duplicate score ticker: {ticker}")
        scores[ticker] = row

    targets: dict[str, float] = {}
    for row in target_rows:
        ticker = _ticker(row.get("ticker"))
        if not ticker:
            continue
        if ticker in targets:
            raise ValueError(f"Duplicate target ticker: {ticker}")
        weight = _number(row.get("weight"))
        if weight < 0:
            raise ValueError(f"Negative target weight for {ticker}")
        if weight <= 1e-12:
            continue
        targets[ticker] = weight

    holdings: dict[str, float] = {}
    for row in holding_rows:
        ticker = _ticker(row.get("symbol") or row.get("ticker"))
        if not ticker:
            continue
        shares = _number(row.get("net_shares", row.get("quantity", 0.0)))
        # Add back SYEP-lent shares: net_shares=0 with shares_lent=-100 is a
        # fully-lent holding the owner still economically holds, not a closed
        # position.
        shares -= _number(row.get("shares_lent", 0.0))
        if abs(shares) <= 1e-12:
            continue
        if ticker in holdings:
            raise ValueError(f"Duplicate holding ticker: {ticker}")
        holdings[ticker] = shares

    pending_orders = {
        ticker
        for value in pending_order_tickers
        if (ticker := _ticker(value))
    }

    rows: list[dict[str, Any]] = []
    for ticker in sorted(set(scores) | set(targets) | set(holdings) | pending_orders):
        score = scores.get(ticker, {})
        is_holding = int(ticker in holdings)
        is_target = int(ticker in targets)
        is_scored = int(ticker in scores)
        is_pending_order = int(ticker in pending_orders)
        investable = _flag(score.get("investable_eligible")) if is_scored else 0
        if is_holding or is_target or is_pending_order:
            tier, priority = "tier0", 0
        elif investable:
            tier, priority = "tier1", 1
        else:
            tier, priority = "tier2", 2
        rows.append(
            {
                "run_as_of": run_as_of,
                "ticker": ticker,
                "tier": tier,
                "priority_rank": priority,
                "is_holding": is_holding,
                "is_target": is_target,
                "is_pending_order": is_pending_order,
                "is_scored": is_scored,
                "investable_eligible": investable,
                "source_pipeline": str(score.get("source_pipeline", "")).strip(),
                "sector": str(score.get("sector", "")).strip(),
                "industry": str(score.get("industry", "")).strip(),
                "rating": str(score.get("rating", "")).strip(),
                "final_score": _number(score.get("final_score")) if is_scored else None,
                "score_confidence": _number(score.get("score_confidence")) if is_scored else None,
                "target_weight": targets.get(ticker, 0.0),
                "net_shares": holdings.get(ticker, 0.0),
                "score_source_asof_date": str(score.get("source_asof_date", "")).strip(),
                "updated_at_utc": updated_at_utc,
            }
        )
    return rows


def _quarantine_legacy_actual_outcome_tables(conn: sqlite3.Connection) -> None:
    tables = {
        str(row['name'])
        for row in conn.execute(
            'SELECT name FROM sqlite_master WHERE type=\'table\''
        ).fetchall()
    }
    old_actual = 'provider_actual_outcomes'
    old_links = 'provider_forecast_outcome_links'
    quarantine_actual = 'provider_actual_outcomes_canary_v1_quarantine'
    quarantine_links = 'provider_forecast_outcome_links_canary_v1_quarantine'
    if old_actual not in tables:
        return
    if quarantine_actual in tables or quarantine_links in tables:
        raise RuntimeError('Legacy outcome quarantine exists beside active v1 tables')
    link_count = 0
    if old_links in tables:
        link_count = int(
            conn.execute(
                'SELECT COUNT(*) FROM provider_forecast_outcome_links'
            ).fetchone()[0]
        )
    if link_count:
        raise RuntimeError('Cannot quarantine legacy outcomes with linked forecast rows')
    with conn:
        if old_links in tables:
            conn.execute(
                'ALTER TABLE provider_forecast_outcome_links RENAME TO '
                'provider_forecast_outcome_links_canary_v1_quarantine'
            )
        conn.execute(
            'ALTER TABLE provider_actual_outcomes RENAME TO '
            'provider_actual_outcomes_canary_v1_quarantine'
        )


def connect_monitor_db(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_sec * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    _quarantine_legacy_actual_outcome_tables(conn)
    return conn


def replace_universe_snapshot(
    conn: sqlite3.Connection,
    *,
    run_as_of: str,
    rows: list[dict[str, Any]],
    source_artifacts: list[dict[str, str]],
) -> None:
    values = [tuple(row[field] for field in UNIVERSE_FIELDS) for row in rows]
    placeholders = ",".join("?" for _ in UNIVERSE_FIELDS)
    columns = ",".join(UNIVERSE_FIELDS)
    with conn:
        conn.execute("DELETE FROM monitor_universe WHERE run_as_of = ?", (run_as_of,))
        conn.execute("DELETE FROM monitor_source_artifacts WHERE run_as_of = ?", (run_as_of,))
        conn.executemany(f"INSERT INTO monitor_universe({columns}) VALUES ({placeholders})", values)
        conn.executemany(
            """
            INSERT INTO monitor_source_artifacts(
                run_as_of, source_role, artifact_path, artifact_sha256,
                manifest_path, manifest_sha256, acceptance
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_as_of,
                    row["source_role"],
                    row["artifact_path"],
                    row["artifact_sha256"],
                    row["manifest_path"],
                    row["manifest_sha256"],
                    row["acceptance"],
                )
                for row in source_artifacts
            ],
        )


def fetch_universe_snapshot(conn: sqlite3.Connection, run_as_of: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {','.join(UNIVERSE_FIELDS)} FROM monitor_universe WHERE run_as_of = ? ORDER BY ticker",
        (run_as_of,),
    ).fetchall()
    return [dict(row) for row in rows]


def _normalized_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    provider = str(row.get("provider", "")).strip().casefold()
    if provider not in {"alpha_vantage", "fmp"}:
        raise ValueError(f"Unsupported estimate provider: {provider!r}")
    endpoint_id = str(row.get("endpoint_id", "")).strip()
    ticker = _ticker(row.get("ticker"))
    fiscal_period_end = str(row.get("fiscal_period_end", "")).strip()
    estimate_type = str(row.get("estimate_type", "")).strip().casefold()
    fetched_at_utc = str(row.get("fetched_at_utc", "")).strip()
    available_at_utc = str(row.get("available_at_utc", "")).strip()
    retrieval_cycle = str(row.get("retrieval_cycle", "")).strip()
    required = {
        "endpoint_id": endpoint_id,
        "ticker": ticker,
        "fiscal_period_end": fiscal_period_end,
        "estimate_type": estimate_type,
        "fetched_at_utc": fetched_at_utc,
        "available_at_utc": available_at_utc,
        "retrieval_cycle": retrieval_cycle,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing estimate snapshot fields: {missing}")
    retention_class = str(row.get("retention_class", "")).strip().casefold()
    if retention_class not in {"provisional_user_authorized", "confirmed"}:
        raise ValueError(f"Invalid retention_class: {retention_class!r}")
    coverage_status = str(row.get("coverage_status", "available")).strip().casefold()
    if coverage_status not in {"available", "missing", "stale", "invalid"}:
        raise ValueError(f"Invalid coverage_status: {coverage_status!r}")
    normalized = {
        "snapshot_run_id": str(row.get("snapshot_run_id", "")).strip() or None,
        "provider": provider,
        "endpoint_id": endpoint_id,
        "ticker": ticker,
        "fiscal_period_end": fiscal_period_end,
        "fiscal_period": str(row.get("fiscal_period", "")).strip(),
        "estimate_type": estimate_type,
        "estimate_average": _optional_number(row.get("estimate_average")),
        "estimate_high": _optional_number(row.get("estimate_high")),
        "estimate_low": _optional_number(row.get("estimate_low")),
        "analyst_count": _optional_integer(row.get("analyst_count")),
        "estimate_average_7_days_ago": _optional_number(row.get("estimate_average_7_days_ago")),
        "estimate_average_30_days_ago": _optional_number(row.get("estimate_average_30_days_ago")),
        "estimate_average_60_days_ago": _optional_number(row.get("estimate_average_60_days_ago")),
        "estimate_average_90_days_ago": _optional_number(row.get("estimate_average_90_days_ago")),
        "revision_up_7_days": _optional_integer(row.get("revision_up_7_days")),
        "revision_down_7_days": _optional_integer(row.get("revision_down_7_days")),
        "revision_up_30_days": _optional_integer(row.get("revision_up_30_days")),
        "revision_down_30_days": _optional_integer(row.get("revision_down_30_days")),
        "currency": str(row.get("currency", "")).strip().upper(),
        "provider_published_at_utc": str(row.get("provider_published_at_utc", "")).strip(),
        "fetched_at_utc": fetched_at_utc,
        "available_at_utc": available_at_utc,
        "retrieval_cycle": retrieval_cycle,
        "source_uid": str(row.get("source_uid", "")).strip(),
        "response_sha256": _require_sha256(row.get("response_sha256"), label="response_sha256"),
        "entitlement_version": str(row.get("entitlement_version", "")).strip(),
        "retention_class": retention_class,
        "coverage_status": coverage_status,
    }
    if not normalized["entitlement_version"]:
        raise ValueError("entitlement_version is required")
    if coverage_status == "available" and all(normalized[field] is None for field in SNAPSHOT_VALUE_FIELDS):
        raise ValueError("Available estimate snapshot has no normalized estimate values")
    identity = {
        key: normalized[key]
        for key in (
            "provider",
            "endpoint_id",
            "ticker",
            "fiscal_period_end",
            "estimate_type",
            "retrieval_cycle",
        )
    }
    normalized_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    normalized["snapshot_id"] = _sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    normalized["normalized_sha256"] = _sha256_text(normalized_json)
    return normalized


def append_estimate_snapshots(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Append normalized snapshots; identical retries are idempotent and drift fails closed."""
    inserted = 0
    duplicates = 0
    columns = [
        "snapshot_id",
        "snapshot_run_id",
        "provider",
        "endpoint_id",
        "ticker",
        "fiscal_period_end",
        "fiscal_period",
        "estimate_type",
        *SNAPSHOT_VALUE_FIELDS,
        "currency",
        "provider_published_at_utc",
        "fetched_at_utc",
        "available_at_utc",
        "retrieval_cycle",
        "source_uid",
        "response_sha256",
        "normalized_sha256",
        "entitlement_version",
        "retention_class",
        "coverage_status",
    ]
    placeholders = ",".join("?" for _ in columns)
    with conn:
        for raw_row in rows:
            row = _normalized_snapshot(raw_row)
            existing = conn.execute(
                "SELECT normalized_sha256 FROM provider_estimate_snapshots WHERE snapshot_id = ?",
                (row["snapshot_id"],),
            ).fetchone()
            if existing is not None:
                if existing["normalized_sha256"] != row["normalized_sha256"]:
                    raise RuntimeError(
                        f"Estimate snapshot identity collision with changed normalized values: {row['snapshot_id']}"
                    )
                duplicates += 1
                continue
            conn.execute(
                f"INSERT INTO provider_estimate_snapshots({','.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
            inserted += 1
    return inserted, duplicates


def _normalized_basis_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    estimate_provider = str(row.get('estimate_provider', '')).strip().casefold()
    if estimate_provider not in {'alpha_vantage', 'fmp'}:
        raise ValueError(f'Unsupported estimate provider: {estimate_provider!r}')
    currency_source = str(row.get('currency_source_provider', '')).strip().casefold()
    if currency_source not in {'fmp', 'manual'}:
        raise ValueError(f'Unsupported currency source: {currency_source!r}')
    ticker = _ticker(row.get('ticker'))
    metric = str(row.get('metric', '')).strip().casefold()
    if metric not in {'eps', 'revenue'}:
        raise ValueError(f'Unsupported metric basis: {metric!r}')
    retrieval_cycle = str(row.get('retrieval_cycle', '')).strip()
    fetched_at = str(row.get('fetched_at_utc', '')).strip()
    available_at = str(row.get('available_at_utc', '')).strip()
    endpoint_id = str(row.get('endpoint_id', '')).strip()
    if not all((ticker, retrieval_cycle, fetched_at, available_at, endpoint_id)):
        raise ValueError('Metric-basis snapshot is missing an identity or PIT field')
    reporting_currency = str(row.get('reporting_currency', '')).strip().upper()
    if reporting_currency and (
        len(reporting_currency) != 3 or not reporting_currency.isalpha()
    ):
        raise ValueError(f'Invalid ISO currency code: {reporting_currency!r}')
    comparison_eligible = _flag(row.get('comparison_eligible'))
    reasons = str(row.get('ineligibility_reasons', '')).strip()
    if comparison_eligible and reasons:
        raise ValueError('Eligible metric basis cannot carry ineligibility reasons')
    if not comparison_eligible and not reasons:
        raise ValueError('Ineligible metric basis requires a reason')
    normalized = {
        'estimate_provider': estimate_provider,
        'currency_source_provider': currency_source,
        'endpoint_id': endpoint_id,
        'ticker': ticker,
        'metric': metric,
        'reporting_currency': reporting_currency,
        'statement_period_end': str(row.get('statement_period_end', '')).strip(),
        'metric_definition': str(row.get('metric_definition', '')).strip(),
        'unit_scale': str(row.get('unit_scale', '')).strip(),
        'per_share_basis': str(row.get('per_share_basis', '')).strip(),
        'currency_semantics_status': str(
            row.get('currency_semantics_status', '')
        ).strip(),
        'definition_semantics_status': str(
            row.get('definition_semantics_status', '')
        ).strip(),
        'comparison_eligible': comparison_eligible,
        'ineligibility_reasons': reasons,
        'fetched_at_utc': fetched_at,
        'available_at_utc': available_at,
        'retrieval_cycle': retrieval_cycle,
        'response_sha256': _require_sha256(
            row.get('response_sha256'), label='response_sha256'
        ),
        'entitlement_version': str(row.get('entitlement_version', '')).strip(),
        'retention_class': str(row.get('retention_class', '')).strip().casefold(),
        'coverage_status': str(row.get('coverage_status', '')).strip().casefold(),
    }
    required_text = (
        'metric_definition',
        'unit_scale',
        'per_share_basis',
        'currency_semantics_status',
        'definition_semantics_status',
        'entitlement_version',
    )
    if any(not normalized[field] for field in required_text):
        raise ValueError('Metric-basis snapshot has blank contract fields')
    if normalized['retention_class'] not in {
        'provisional_user_authorized',
        'confirmed',
    }:
        raise ValueError('Invalid basis retention class')
    if normalized['coverage_status'] not in {'available', 'missing', 'stale', 'invalid'}:
        raise ValueError('Invalid basis coverage status')
    identity = {
        key: normalized[key]
        for key in ('estimate_provider', 'ticker', 'metric', 'retrieval_cycle')
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(',', ':'))
    normalized['basis_snapshot_id'] = _sha256_text(
        json.dumps(identity, sort_keys=True, separators=(',', ':'))
    )
    normalized['normalized_sha256'] = _sha256_text(payload)
    return normalized


def append_metric_basis_snapshots(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    columns = [
        'basis_snapshot_id',
        *BASIS_FIELDS[:18],
        'normalized_sha256',
        *BASIS_FIELDS[18:],
    ]
    column_sql = ','.join(columns)
    placeholders = ','.join('?' for _ in columns)
    with conn:
        for raw_row in rows:
            row = _normalized_basis_snapshot(raw_row)
            existing = conn.execute(
                'SELECT normalized_sha256 FROM provider_metric_basis_snapshots '
                'WHERE basis_snapshot_id=?',
                (row['basis_snapshot_id'],),
            ).fetchone()
            if existing is not None:
                if existing['normalized_sha256'] != row['normalized_sha256']:
                    raise RuntimeError('Metric-basis identity collision with changed values')
                duplicates += 1
                continue
            conn.execute(
                f'INSERT INTO provider_metric_basis_snapshots({column_sql}) '
                f'VALUES ({placeholders})',
                tuple(row[column] for column in columns),
            )
            inserted += 1
    return inserted, duplicates


def _normalized_fiscal_period_resolution(row: dict[str, Any]) -> dict[str, Any]:
    source_provider = str(row.get('source_provider', '')).strip().casefold()
    if source_provider != 'alpha_vantage':
        raise ValueError('Fiscal-period resolution source must be alpha_vantage')
    endpoint_id = str(row.get('endpoint_id', '')).strip()
    ticker = _ticker(row.get('ticker'))
    report_date = str(row.get('report_date', '')).strip()
    fiscal_period_end = str(row.get('fiscal_period_end', '')).strip()
    retrieval_cycle = str(row.get('retrieval_cycle', '')).strip()
    fetched_at = str(row.get('fetched_at_utc', '')).strip()
    available_at = str(row.get('available_at_utc', '')).strip()
    if not all(
        (
            endpoint_id,
            ticker,
            report_date,
            fiscal_period_end,
            retrieval_cycle,
            fetched_at,
            available_at,
        )
    ):
        raise ValueError('Fiscal-period resolution is missing an identity or PIT field')
    reported_on = date.fromisoformat(report_date)
    period_ended_on = date.fromisoformat(fiscal_period_end)
    if period_ended_on > reported_on:
        raise ValueError('Fiscal period cannot end after its report date')
    datetime.fromisoformat(fetched_at)
    datetime.fromisoformat(available_at)
    resolution_eligible = _flag(row.get('resolution_eligible'))
    reasons = str(row.get('ineligibility_reasons', '')).strip()
    if resolution_eligible and reasons:
        raise ValueError('Eligible fiscal-period resolution cannot carry reasons')
    if not resolution_eligible and not reasons:
        raise ValueError('Ineligible fiscal-period resolution requires a reason')
    normalized = {
        'source_provider': source_provider,
        'endpoint_id': endpoint_id,
        'ticker': ticker,
        'report_date': report_date,
        'fiscal_period_end': fiscal_period_end,
        'fiscal_period': str(row.get('fiscal_period', '')).strip().casefold(),
        'report_time': str(row.get('report_time', '')).strip().casefold(),
        'resolution_status': str(row.get('resolution_status', '')).strip(),
        'resolution_eligible': resolution_eligible,
        'ineligibility_reasons': reasons,
        'fetched_at_utc': fetched_at,
        'available_at_utc': available_at,
        'retrieval_cycle': retrieval_cycle,
        'response_sha256': _require_sha256(
            row.get('response_sha256'), label='response_sha256'
        ),
        'entitlement_version': str(row.get('entitlement_version', '')).strip(),
        'retention_class': str(row.get('retention_class', '')).strip().casefold(),
        'coverage_status': str(row.get('coverage_status', '')).strip().casefold(),
    }
    if normalized['fiscal_period'] != 'quarterly':
        raise ValueError('Only exact quarterly fiscal-period resolutions are supported')
    if not normalized['resolution_status'] or not normalized['entitlement_version']:
        raise ValueError('Fiscal-period resolution is missing contract status')
    if normalized['retention_class'] not in {
        'provisional_user_authorized',
        'confirmed',
    }:
        raise ValueError('Invalid fiscal-period resolution retention class')
    if normalized['coverage_status'] not in {'available', 'missing', 'stale', 'invalid'}:
        raise ValueError('Invalid fiscal-period resolution coverage status')
    identity = {
        key: normalized[key]
        for key in (
            'source_provider',
            'ticker',
            'report_date',
            'fiscal_period_end',
            'retrieval_cycle',
        )
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(',', ':'))
    normalized['resolution_id'] = _sha256_text(
        json.dumps(identity, sort_keys=True, separators=(',', ':'))
    )
    normalized['normalized_sha256'] = _sha256_text(payload)
    return normalized


def append_fiscal_period_resolutions(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    columns = [
        'resolution_id',
        *FISCAL_PERIOD_RESOLUTION_FIELDS[:14],
        'normalized_sha256',
        *FISCAL_PERIOD_RESOLUTION_FIELDS[14:],
    ]
    column_sql = ','.join(columns)
    placeholders = ','.join('?' for _ in columns)
    with conn:
        for raw_row in rows:
            row = _normalized_fiscal_period_resolution(raw_row)
            existing = conn.execute(
                'SELECT normalized_sha256 FROM provider_fiscal_period_resolutions '
                'WHERE resolution_id=?',
                (row['resolution_id'],),
            ).fetchone()
            if existing is not None:
                if existing['normalized_sha256'] != row['normalized_sha256']:
                    raise RuntimeError(
                        'Fiscal-period resolution identity collision with changed values'
                    )
                duplicates += 1
                continue
            conn.execute(
                f'INSERT INTO provider_fiscal_period_resolutions({column_sql}) '
                f'VALUES ({placeholders})',
                tuple(row[column] for column in columns),
            )
            inserted += 1
    return inserted, duplicates


def _normalized_actual_outcome(row: dict[str, Any]) -> dict[str, Any]:
    provider = str(row.get('provider', '')).strip().casefold()
    if provider not in {'alpha_vantage', 'fmp'}:
        raise ValueError(f'Unsupported actual-outcome provider: {provider!r}')
    ticker = _ticker(row.get('ticker'))
    metric = str(row.get('metric', '')).strip().casefold()
    if metric not in {'eps', 'revenue'}:
        raise ValueError(f'Unsupported actual-outcome metric: {metric!r}')
    report_date = str(row.get('report_date', '')).strip()
    fiscal_period_end = str(row.get('fiscal_period_end', '')).strip()
    outcome_period_status = str(row.get('outcome_period_status', '')).strip()
    retrieval_cycle = str(row.get('retrieval_cycle', '')).strip()
    endpoint_id = str(row.get('endpoint_id', '')).strip()
    fetched_at = str(row.get('fetched_at_utc', '')).strip()
    available_at = str(row.get('available_at_utc', '')).strip()
    if not all(
        (ticker, metric, report_date, retrieval_cycle, endpoint_id, fetched_at, available_at)
    ):
        raise ValueError('Actual outcome is missing an identity or PIT field')
    datetime.fromisoformat(fetched_at)
    datetime.fromisoformat(available_at)
    actual_value = _number(row.get('actual_value'))
    reporting_currency = str(row.get('reporting_currency', '')).strip().upper()
    if reporting_currency and (
        len(reporting_currency) != 3 or not reporting_currency.isalpha()
    ):
        raise ValueError(f'Invalid actual-outcome currency: {reporting_currency!r}')
    evaluation_eligible = _flag(row.get('evaluation_eligible'))
    reasons = str(row.get('ineligibility_reasons', '')).strip()
    if evaluation_eligible and reasons:
        raise ValueError('Eligible actual outcome cannot carry ineligibility reasons')
    if not evaluation_eligible and not reasons:
        raise ValueError('Ineligible actual outcome requires a reason')
    normalized = {
        'provider': provider,
        'endpoint_id': endpoint_id,
        'ticker': ticker,
        'report_date': report_date,
        'fiscal_period_end': fiscal_period_end,
        'outcome_period_status': outcome_period_status,
        'metric': metric,
        'actual_value': actual_value,
        'reporting_currency': reporting_currency,
        'metric_basis_id': str(row.get('metric_basis_id', '')).strip(),
        'metric_basis_status': str(row.get('metric_basis_status', '')).strip(),
        'provider_updated_at_raw': str(row.get('provider_updated_at_raw', '')).strip(),
        'provider_published_at_utc': str(
            row.get('provider_published_at_utc', '')
        ).strip(),
        'fetched_at_utc': fetched_at,
        'available_at_utc': available_at,
        'retrieval_cycle': retrieval_cycle,
        'response_sha256': _require_sha256(
            row.get('response_sha256'), label='response_sha256'
        ),
        'entitlement_version': str(row.get('entitlement_version', '')).strip(),
        'retention_class': str(row.get('retention_class', '')).strip().casefold(),
        'coverage_status': str(row.get('coverage_status', '')).strip().casefold(),
        'evaluation_eligible': evaluation_eligible,
        'ineligibility_reasons': reasons,
    }
    if (
        not normalized['metric_basis_status']
        or not normalized['outcome_period_status']
        or not normalized['entitlement_version']
    ):
        raise ValueError('Actual outcome is missing basis or entitlement status')
    if normalized['retention_class'] not in {
        'provisional_user_authorized',
        'confirmed',
    }:
        raise ValueError('Invalid actual-outcome retention class')
    if normalized['coverage_status'] not in {'available', 'missing', 'stale', 'invalid'}:
        raise ValueError('Invalid actual-outcome coverage status')
    identity = {
        key: normalized[key]
        for key in (
            'provider',
            'endpoint_id',
            'ticker',
            'report_date',
            'metric',
            'retrieval_cycle',
        )
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(',', ':'))
    normalized['outcome_id'] = _sha256_text(
        json.dumps(identity, sort_keys=True, separators=(',', ':'))
    )
    normalized['normalized_sha256'] = _sha256_text(payload)
    return normalized


def verify_actual_outcome_chain(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        'SELECT row_sequence,previous_row_sha256,row_sha256,outcome_id,normalized_sha256 '
        'FROM provider_actual_outcomes_v2 ORDER BY row_sequence'
    ).fetchall()
    errors: list[str] = []
    previous = '0' * 64
    for expected_sequence, row in enumerate(rows, start=1):
        sequence = int(row['row_sequence'])
        if sequence != expected_sequence:
            errors.append(
                f'outcome_chain_sequence_gap:{expected_sequence}:{sequence}'
            )
        if row['previous_row_sha256'] != previous:
            errors.append(f'outcome_chain_previous_mismatch:{sequence}')
        chain_payload = {
            'row_sequence': sequence,
            'previous_row_sha256': row['previous_row_sha256'],
            'outcome_id': row['outcome_id'],
            'normalized_sha256': row['normalized_sha256'],
        }
        expected_hash = _sha256_text(
            json.dumps(chain_payload, sort_keys=True, separators=(',', ':'))
        )
        if row['row_sha256'] != expected_hash:
            errors.append(f'outcome_chain_hash_mismatch:{sequence}')
        previous = str(row['row_sha256'])
    return errors


def append_actual_outcomes(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    chain_errors = verify_actual_outcome_chain(conn)
    if chain_errors:
        raise RuntimeError(f'Actual-outcome ledger integrity failure: {chain_errors}')
    normalized_rows = sorted(
        (_normalized_actual_outcome(row) for row in rows),
        key=lambda row: str(row['outcome_id']),
    )
    inserted = 0
    duplicates = 0
    last = conn.execute(
        'SELECT row_sequence,row_sha256 FROM provider_actual_outcomes_v2 '
        'ORDER BY row_sequence DESC LIMIT 1'
    ).fetchone()
    sequence = int(last['row_sequence']) if last is not None else 0
    previous = str(last['row_sha256']) if last is not None else '0' * 64
    columns = [
        'outcome_id',
        'row_sequence',
        'previous_row_sha256',
        'row_sha256',
        *ACTUAL_OUTCOME_VALUE_FIELDS[:17],
        'normalized_sha256',
        *ACTUAL_OUTCOME_VALUE_FIELDS[17:],
    ]
    column_sql = ','.join(columns)
    placeholders = ','.join('?' for _ in columns)
    with conn:
        for row in normalized_rows:
            existing = conn.execute(
                'SELECT normalized_sha256 FROM provider_actual_outcomes_v2 WHERE outcome_id=?',
                (row['outcome_id'],),
            ).fetchone()
            if existing is not None:
                if existing['normalized_sha256'] != row['normalized_sha256']:
                    raise RuntimeError('Actual-outcome identity collision with changed values')
                duplicates += 1
                continue
            sequence += 1
            chain_payload = {
                'row_sequence': sequence,
                'previous_row_sha256': previous,
                'outcome_id': row['outcome_id'],
                'normalized_sha256': row['normalized_sha256'],
            }
            row['row_sequence'] = sequence
            row['previous_row_sha256'] = previous
            row['row_sha256'] = _sha256_text(
                json.dumps(chain_payload, sort_keys=True, separators=(',', ':'))
            )
            conn.execute(
                f'INSERT INTO provider_actual_outcomes_v2({column_sql}) '
                f'VALUES ({placeholders})',
                tuple(row[column] for column in columns),
            )
            previous = str(row['row_sha256'])
            inserted += 1
    final_errors = verify_actual_outcome_chain(conn)
    if final_errors:
        raise RuntimeError(f'Actual-outcome ledger failed post-append verification: {final_errors}')
    return inserted, duplicates


def record_snapshot_dependencies(
    conn: sqlite3.Connection,
    *,
    artifact_path: str,
    artifact_sha256: str,
    snapshot_ids: Iterable[str],
) -> int:
    artifact_digest = _require_sha256(artifact_sha256, label="artifact_sha256")
    unique_ids = sorted(set(snapshot_ids))
    if not unique_ids:
        return 0
    recorded_at = utc_now()
    inserted = 0
    with conn:
        for snapshot_id in unique_ids:
            snapshot = conn.execute(
                "SELECT provider, fetched_at_utc FROM provider_estimate_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise ValueError(f"Unknown provider snapshot dependency: {snapshot_id}")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO provider_snapshot_dependencies(
                    artifact_path, artifact_sha256, snapshot_id, provider,
                    fetched_at_utc, status, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'valid', ?)
                """,
                (
                    artifact_path,
                    artifact_digest,
                    snapshot_id,
                    snapshot["provider"],
                    snapshot["fetched_at_utc"],
                    recorded_at,
                ),
            )
            inserted += int(cursor.rowcount > 0)
    return inserted


def supersede_artifact_dependencies(
    conn: sqlite3.Connection,
    *,
    artifact_path: str,
    current_artifact_sha256: str,
    reason: str = "artifact_superseded",
) -> int:
    """Invalidate prior hashes for an artifact path before recording its current lineage."""
    current_digest = _require_sha256(
        current_artifact_sha256,
        label="current_artifact_sha256",
    )
    invalidated_at = utc_now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE provider_snapshot_dependencies
            SET status='invalidated', invalidated_at_utc=?, invalidation_reason=?
            WHERE artifact_path=? AND artifact_sha256<>? AND status='valid'
            """,
            (invalidated_at, reason.strip(), artifact_path, current_digest),
        )
    return int(cursor.rowcount)


def artifact_snapshot_dependency_errors(
    conn: sqlite3.Connection, *, artifact_path: str, artifact_sha256: str
) -> list[str]:
    """Return fail-closed errors for a provider-backed artifact's exact snapshot lineage."""
    artifact_digest = _require_sha256(artifact_sha256, label="artifact_sha256")
    dependencies = conn.execute(
        """
        SELECT snapshot_id, status FROM provider_snapshot_dependencies
        WHERE artifact_path = ? AND artifact_sha256 = ?
        ORDER BY snapshot_id
        """,
        (artifact_path, artifact_digest),
    ).fetchall()
    if not dependencies:
        return ["provider_snapshot_dependencies_missing"]
    errors: list[str] = []
    invalidated = [row["snapshot_id"] for row in dependencies if row["status"] != "valid"]
    if invalidated:
        errors.append(f"provider_snapshot_dependencies_invalidated={len(invalidated)}")
    valid_ids = [row["snapshot_id"] for row in dependencies if row["status"] == "valid"]
    missing = 0
    for batch in _chunks(valid_ids, 400):
        placeholders = ",".join("?" for _ in batch)
        found = conn.execute(
            f"SELECT COUNT(*) FROM provider_estimate_snapshots WHERE snapshot_id IN ({placeholders})",
            tuple(batch),
        ).fetchone()[0]
        missing += len(batch) - int(found)
    if missing:
        errors.append(f"provider_snapshot_dependencies_missing_rows={missing}")
    return errors


def plan_provider_purge(conn: sqlite3.Connection, *, provider: str, from_date: str, to_date: str) -> dict[str, Any]:
    provider_name = provider.strip().casefold()
    if provider_name not in {"alpha_vantage", "fmp"}:
        raise ValueError(f"Unsupported purge provider: {provider_name!r}")
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    snapshots = conn.execute(
        """
        SELECT snapshot_id FROM provider_estimate_snapshots
        WHERE provider = ? AND substr(fetched_at_utc, 1, 10) BETWEEN ? AND ?
        ORDER BY snapshot_id
        """,
        (provider_name, from_date, to_date),
    ).fetchall()
    snapshot_ids = [str(row["snapshot_id"]) for row in snapshots]
    dependencies: list[dict[str, Any]] = []
    for batch in _chunks(snapshot_ids, 400):
        placeholders = ",".join("?" for _ in batch)
        dependencies.extend(
            dict(row)
            for row in conn.execute(
                "SELECT artifact_path, artifact_sha256, snapshot_id FROM "
                f"provider_snapshot_dependencies WHERE status = 'valid' "
                f"AND snapshot_id IN ({placeholders}) ORDER BY artifact_path, snapshot_id",
                tuple(batch),
            ).fetchall()
        )
    artifacts = sorted({row["artifact_path"] for row in dependencies})
    return {
        "provider": provider_name,
        "from_date": from_date,
        "to_date": to_date,
        "snapshot_ids": snapshot_ids,
        "snapshot_count": len(snapshot_ids),
        "dependency_count": len(dependencies),
        "dependent_artifacts": artifacts,
    }


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def execute_provider_purge(
    conn: sqlite3.Connection,
    *,
    provider: str,
    from_date: str,
    to_date: str,
    reason: str,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("A non-empty purge reason is required")
    plan = plan_provider_purge(conn, provider=provider, from_date=from_date, to_date=to_date)
    snapshot_ids = list(plan["snapshot_ids"])
    now = utc_now()
    invalidated = 0
    with conn:
        for batch in _chunks(snapshot_ids, 400):
            placeholders = ",".join("?" for _ in batch)
            cursor = conn.execute(
                "UPDATE provider_snapshot_dependencies SET status = 'invalidated', "
                "invalidated_at_utc = ?, invalidation_reason = ? "
                f"WHERE status = 'valid' AND snapshot_id IN ({placeholders})",
                (now, reason.strip(), *batch),
            )
            invalidated += cursor.rowcount
            conn.execute(
                f"DELETE FROM provider_estimate_snapshots WHERE snapshot_id IN ({placeholders})",
                tuple(batch),
            )
        purge_event_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO provider_purge_events(
                purge_event_id, provider, from_date, to_date, requested_at_utc,
                executed_at_utc, reason, deleted_snapshot_count,
                invalidated_dependency_count, invalidated_artifacts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                purge_event_id,
                plan["provider"],
                from_date,
                to_date,
                now,
                now,
                reason.strip(),
                plan["snapshot_count"],
                invalidated,
                json.dumps(plan["dependent_artifacts"], separators=(",", ":")),
            ),
        )
    return {
        **plan,
        "purge_event_id": purge_event_id,
        "executed_at_utc": now,
        "invalidated_dependency_count": invalidated,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def writer_lock(path: Path, *, timeout_sec: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    host = socket.gethostname()
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    payload = {
        "pid": os.getpid(),
        "host": host,
        "created_at_utc": utc_now(),
        "token": token,
    }
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Unreadable monitor writer lock: {path}") from exc
            existing_pid = int(existing.get("pid", -1))
            if existing.get("host") == host and not _pid_alive(existing_pid):
                path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Monitor writer lock is held by pid={existing_pid}: {path}")
            time.sleep(0.1)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        break
    try:
        yield
    finally:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Monitor writer lock changed while held: {path}") from exc
        if existing.get("token") != token:
            raise RuntimeError(f"Monitor writer lock ownership changed: {path}")
        path.unlink()


@contextmanager
def database_writer_lock(
    database_path: Path, *, timeout_sec: float = 30.0
) -> Iterator[None]:
    lock_path = database_path.with_suffix(database_path.suffix + ".writer.lock")
    with writer_lock(lock_path, timeout_sec=timeout_sec):
        yield


def run_selftest(tmp_dir: Path) -> None:
    rows = build_universe_rows(
        run_as_of="2026-07-24",
        score_rows=[
            {
                "ticker": "AAA",
                "investable_eligible": "1",
                "final_score": "0.1",
                "score_confidence": "0.8",
            },
            {
                "ticker": "BBB",
                "investable_eligible": "0",
                "final_score": "-0.1",
                "score_confidence": "0.5",
            },
            {
                "ticker": "CCC",
                "investable_eligible": "1",
                "final_score": "0.05",
                "score_confidence": "0.7",
            },
        ],
        target_rows=[{"ticker": "BBB", "weight": "0.1"}, {"ticker": "CASH", "weight": "0.9"}],
        holding_rows=[{"symbol": "DDD", "net_shares": "10"}],
        updated_at_utc="2026-07-24T22:00:00+00:00",
    )
    tiers = {row["ticker"]: row["tier"] for row in rows}
    assert tiers == {"AAA": "tier1", "BBB": "tier0", "CCC": "tier1", "DDD": "tier0"}
    db_path = tmp_dir / "monitor.sqlite"
    lock_path = tmp_dir / ".monitor.lock"
    with writer_lock(lock_path, timeout_sec=0.1):
        conn = connect_monitor_db(db_path, timeout_sec=1.0)
        try:
            replace_universe_snapshot(conn, run_as_of="2026-07-24", rows=rows, source_artifacts=[])
            assert fetch_universe_snapshot(conn, "2026-07-24") == rows
            snapshot = {
                "provider": "alpha_vantage",
                "endpoint_id": "earnings_estimates",
                "ticker": "AAA",
                "fiscal_period_end": "2026-12-31",
                "fiscal_period": "annual",
                "estimate_type": "eps",
                "estimate_average": 2.5,
                "analyst_count": 8,
                "fetched_at_utc": "2026-07-31T22:00:00+00:00",
                "available_at_utc": "2026-07-31T22:00:00+00:00",
                "retrieval_cycle": "2026-07-31-eod",
                "response_sha256": "a" * 64,
                "entitlement_version": "provider_entitlements_v1",
                "retention_class": "provisional_user_authorized",
                "coverage_status": "available",
            }
            assert append_estimate_snapshots(conn, [snapshot]) == (1, 0)
            assert append_estimate_snapshots(conn, [snapshot]) == (0, 1)
            snapshot_id = conn.execute("SELECT snapshot_id FROM provider_estimate_snapshots").fetchone()["snapshot_id"]
            assert (
                record_snapshot_dependencies(
                    conn,
                    artifact_path="output/test.json",
                    artifact_sha256="b" * 64,
                    snapshot_ids=[snapshot_id],
                )
                == 1
            )
            assert (
                supersede_artifact_dependencies(
                    conn,
                    artifact_path="output/test.json",
                    current_artifact_sha256="c" * 64,
                )
                == 1
            )
            assert (
                record_snapshot_dependencies(
                    conn,
                    artifact_path="output/test.json",
                    artifact_sha256="c" * 64,
                    snapshot_ids=[snapshot_id],
                )
                == 1
            )
            plan = plan_provider_purge(
                conn,
                provider="alpha_vantage",
                from_date="2026-07-31",
                to_date="2026-07-31",
            )
            assert plan["snapshot_count"] == 1
            assert plan["dependent_artifacts"] == ["output/test.json"]
            result = execute_provider_purge(
                conn,
                provider="alpha_vantage",
                from_date="2026-07-31",
                to_date="2026-07-31",
                reason="selftest",
            )
            assert result["invalidated_dependency_count"] == 1
            assert conn.execute("SELECT COUNT(*) FROM provider_estimate_snapshots").fetchone()[0] == 0
            assert {
                row["status"]
                for row in conn.execute("SELECT status FROM provider_snapshot_dependencies")
            } == {"invalidated"}
            assert artifact_snapshot_dependency_errors(
                conn,
                artifact_path="output/test.json",
                artifact_sha256="b" * 64,
            ) == ["provider_snapshot_dependencies_invalidated=1"]
        finally:
            conn.close()
    assert not lock_path.exists()
    sentinel_path = tmp_dir / "sentinel.sqlite"
    sentinel_path.write_bytes(b"sqlite-sentinel")
    sentinel_lock = sentinel_path.with_suffix(sentinel_path.suffix + ".writer.lock")
    with database_writer_lock(sentinel_path, timeout_sec=0.1):
        assert sentinel_path.read_bytes() == b"sqlite-sentinel"
        assert sentinel_lock.is_file()
    assert sentinel_path.read_bytes() == b"sqlite-sentinel"
    assert not sentinel_lock.exists()
