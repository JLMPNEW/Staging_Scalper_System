"""Optional intraday liquidity panel helpers.

The portfolio layer keeps the default transaction-cost model independent of any
broker connection. When enabled, this module supports a separate overnight IBKR
historical BID_ASK collection step that writes auditable CSV artifacts and
stores them in the portfolio-owned SQLite database.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from portfolio_layer.core.config import cfg_get
from portfolio_layer.core.contracts import read_csv
from portfolio_layer.core.db import utc_now


IB_SPREAD_SAMPLE_FIELDS = [
    "as_of_date",
    "ticker",
    "query_symbol",
    "target_time_et",
    "bar_date_et",
    "bar_timestamp_et",
    "bar_size",
    "bid",
    "ask",
    "midpoint",
    "spread_bps",
    "half_spread_bps",
    "source",
    "status",
    "reason",
]

SPREAD_SNAPSHOT_FIELDS = [
    "as_of_date",
    "ticker",
    "requested_sample_count",
    "valid_sample_count",
    "latest_sample_date_et",
    "latest_sample_age_days",
    "median_half_spread_bps",
    "max_half_spread_bps",
    "min_half_spread_bps",
    "spread_source",
    "spread_status",
    "spread_reason",
]

LIQUIDITY_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ib_spread_samples (
    as_of_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    query_symbol TEXT,
    target_time_et TEXT NOT NULL,
    bar_date_et TEXT,
    bar_timestamp_et TEXT,
    bar_size TEXT,
    bid REAL,
    ask REAL,
    midpoint REAL,
    spread_bps REAL,
    half_spread_bps REAL,
    source TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker, target_time_et)
);

CREATE TABLE IF NOT EXISTS spread_snapshot (
    as_of_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    requested_sample_count INTEGER,
    valid_sample_count INTEGER,
    latest_sample_date_et TEXT,
    latest_sample_age_days INTEGER,
    median_half_spread_bps REAL,
    max_half_spread_bps REAL,
    min_half_spread_bps REAL,
    spread_source TEXT,
    spread_status TEXT NOT NULL,
    spread_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS spread_snapshot_runs (
    as_of_date TEXT PRIMARY KEY,
    provider TEXT,
    generated_at TEXT,
    universe_source TEXT,
    requested_tickers INTEGER,
    ok_tickers INTEGER,
    fallback_tickers INTEGER,
    failed_tickers INTEGER,
    sample_rows INTEGER,
    snapshot_rows INTEGER,
    samples_sha256 TEXT,
    snapshot_sha256 TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
"""


def finite_float(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return parsed


def liquidity_config(config: dict[str, Any]) -> dict[str, Any]:
    block = cfg_get(config, "liquidity_panel", {}) or {}
    return block if isinstance(block, dict) else {}


def enhanced_liquidity_enabled(config: dict[str, Any]) -> bool:
    return bool(cfg_get(config, "liquidity_panel.enhanced_intraday_enabled", False))


def spread_source_policy(config: dict[str, Any]) -> str:
    policy = str(cfg_get(config, "transaction_costs.spread_source", "auto")).strip().lower()
    allowed = {"auto", "config_default", "liquidity_panel"}
    if policy not in allowed:
        raise ValueError(f"transaction_costs.spread_source must be one of {sorted(allowed)}, got {policy!r}")
    return policy


def liquidity_panel_active(config: dict[str, Any]) -> bool:
    """True when the run should produce/validate/consume the enhanced spread panel."""
    policy = spread_source_policy(config)
    if policy == "config_default":
        return False
    if policy == "liquidity_panel":
        return True
    return enhanced_liquidity_enabled(config)


def configured_fallback_half_spread_bps(config: dict[str, Any]) -> float:
    value = cfg_get(
        config,
        "liquidity_panel.fallback_half_spread_bps",
        cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
    )
    parsed = finite_float(value, name="liquidity_panel.fallback_half_spread_bps")
    if parsed < 0:
        raise ValueError(f"liquidity_panel.fallback_half_spread_bps must be non-negative, got {parsed}")
    return parsed


def liquidity_half_spread_fail_bps(config: dict[str, Any]) -> float:
    value = cfg_get(config, "liquidity_panel.audit.half_spread_fail_bps", 1000.0)
    parsed = finite_float(value, name="liquidity_panel.audit.half_spread_fail_bps")
    if parsed <= 0:
        raise ValueError(f"liquidity_panel.audit.half_spread_fail_bps must be positive, got {parsed}")
    return parsed


def parse_sample_times(config: dict[str, Any]) -> list[str]:
    raw = cfg_get(config, "liquidity_panel.sample_times_et", ["11:00", "13:30", "15:30"])
    if not isinstance(raw, list) or not raw:
        raise ValueError("liquidity_panel.sample_times_et must be a non-empty list")
    out: list[str] = []
    for value in raw:
        text = str(value).strip()
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid liquidity sample time {text!r}; expected HH:MM") from exc
        out.append(text)
    return out


def active_symbol_for_ticker(config: dict[str, Any], ticker: str, as_of: str) -> tuple[str, dict[str, Any] | None]:
    """Return the IB/liquidity query symbol for a contract ticker.

    Broker APIs can use class-share symbols that would be wrong for Yahoo/risk
    price fetches. Prefer liquidity_panel.ticker_aliases, then fall back to the
    risk-panel alias map for true same-issuer ticker migrations.
    """
    key = str(ticker).strip().upper()
    alias_rows: list[dict[str, Any]] = []
    for aliases in [
        cfg_get(config, "liquidity_panel.ticker_aliases", {}) or {},
        cfg_get(config, "risk_panel.ticker_aliases", {}) or {},
    ]:
        if isinstance(aliases, dict):
            for raw_key, raw_value in aliases.items():
                if not isinstance(raw_value, dict):
                    continue
                row = dict(raw_value)
                row.setdefault("ticker", raw_key)
                alias_rows.append(row)
        elif isinstance(aliases, list):
            alias_rows.extend(raw for raw in aliases if isinstance(raw, dict))
    run_date = date.fromisoformat(as_of)
    best: dict[str, Any] | None = None
    best_effective = date.min
    for raw in alias_rows:
        alias_ticker = str(raw.get("ticker", "")).strip().upper()
        if alias_ticker != key:
            continue
        effective_raw = str(raw.get("effective_date", "")).strip()
        effective = date.min
        if effective_raw:
            try:
                effective = date.fromisoformat(effective_raw)
            except ValueError:
                continue
            if effective > run_date:
                continue
        if best is None or effective >= best_effective:
            best = raw
            best_effective = effective
    if not best:
        return key, None
    active = str(best.get("ib_symbol") or best.get("query_symbol") or best.get("active_ticker") or key).strip().upper()
    return active, best


def load_spread_snapshot(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker:
            out[ticker] = row
    return out


def summarize_spread_samples(
    rows: Sequence[dict[str, Any]],
    *,
    as_of: str,
    tickers: Iterable[str],
    sample_times: Sequence[str],
    min_valid_samples: int,
    max_stale_days: int,
    max_half_spread_bps: float,
    fallback_half_spread_bps: float,
    allow_fallback: bool,
) -> list[dict[str, Any]]:
    """Collapse per-target-time samples into one half-spread per ticker."""
    fallback_half_spread_bps = finite_float(fallback_half_spread_bps, name="fallback_half_spread_bps")
    if fallback_half_spread_bps < 0:
        raise ValueError("fallback_half_spread_bps must be non-negative")
    max_half_spread_bps = finite_float(max_half_spread_bps, name="max_half_spread_bps")
    if max_half_spread_bps <= 0:
        raise ValueError("max_half_spread_bps must be positive")
    min_valid_samples = max(1, int(min_valid_samples))
    max_stale_days = max(0, int(max_stale_days))
    sample_count = len(sample_times)
    as_of_date = date.fromisoformat(as_of)

    grouped: dict[str, list[dict[str, Any]]] = {str(t).strip().upper(): [] for t in tickers if str(t).strip()}
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker:
            grouped.setdefault(ticker, []).append(row)

    snapshot: list[dict[str, Any]] = []
    for ticker in sorted(grouped):
        valid: list[tuple[float, date]] = []
        extreme: list[float] = []
        reasons: list[str] = []
        hard_fail_reasons: list[str] = []
        for row in grouped[ticker]:
            status = str(row.get("status", "")).strip().lower()
            reason = str(row.get("reason", "")).strip()
            if reason:
                reasons.append(reason)
            try:
                half = finite_float(row.get("half_spread_bps"), name=f"{ticker}.half_spread_bps")
            except ValueError:
                half = None
            raw_day = str(row.get("bar_date_et", "")).strip()
            try:
                sample_day = date.fromisoformat(raw_day)
            except ValueError:
                sample_day = None
            age_ok = sample_day is not None and 0 <= (as_of_date - sample_day).days <= max_stale_days
            if status != "ok":
                # extreme samples marked invalid by the collector stay VISIBLE to the audit as a
                # hard failure (never silently replaced by the fallback default) — but only while
                # fresh: a stale extreme print must not poison the name forever
                if ("half_spread_bps>=" in reason or "spread_bps>=" in reason) and (sample_day is None or age_ok):
                    hard_fail_reasons.append(reason)
                    if half is not None and age_ok:
                        extreme.append(half)
                continue
            if half is None or sample_day is None:
                continue
            # boundary matches the collector: a half-spread AT the cap is extreme, not valid
            if 0 <= half < max_half_spread_bps and age_ok:
                valid.append((half, sample_day))
            elif half >= max_half_spread_bps and age_ok:
                hard_fail_reasons.append(f"half_spread_bps>={max_half_spread_bps:g}")
                extreme.append(half)
        if len(valid) >= min_valid_samples:
            values = [half for half, _sample_day in valid]
            latest_day = max(sample_day for _half, sample_day in valid)
            latest_age = (as_of_date - latest_day).days
            status = "ok" if latest_age == 0 else "ok_latest_available"
            snapshot.append({
                "as_of_date": as_of,
                "ticker": ticker,
                "requested_sample_count": sample_count,
                "valid_sample_count": len(valid),
                "latest_sample_date_et": latest_day.isoformat(),
                "latest_sample_age_days": latest_age,
                "median_half_spread_bps": round(float(statistics.median(values)), 6),
                "max_half_spread_bps": round(max(values), 6),
                "min_half_spread_bps": round(min(values), 6),
                "spread_source": "ibkr_historical_bid_ask",
                "spread_status": status,
                "spread_reason": "" if latest_age == 0 else f"latest_available_age_days={latest_age}",
            })
            continue

        hard_fail = bool(hard_fail_reasons) and len(valid) < min_valid_samples
        reason = "insufficient_valid_samples"
        if hard_fail:
            reason = ";".join(sorted(set(hard_fail_reasons))[:3])
        if not hard_fail and not valid and reasons:
            reason = ";".join(sorted(set(reasons))[:3])
        if allow_fallback and not hard_fail:
            snapshot.append({
                "as_of_date": as_of,
                "ticker": ticker,
                "requested_sample_count": sample_count,
                "valid_sample_count": len(valid),
                "latest_sample_date_et": "",
                "latest_sample_age_days": "",
                "median_half_spread_bps": round(fallback_half_spread_bps, 6),
                "max_half_spread_bps": round(fallback_half_spread_bps, 6),
                "min_half_spread_bps": round(fallback_half_spread_bps, 6),
                "spread_source": "config_default",
                "spread_status": "fallback",
                "spread_reason": reason,
            })
        else:
            snapshot.append({
                "as_of_date": as_of,
                "ticker": ticker,
                "requested_sample_count": sample_count,
                "valid_sample_count": len(valid),
                "latest_sample_date_et": "",
                "latest_sample_age_days": "",
                # a hard-failed name carries its OBSERVED extreme spread so the audit's
                # extreme-spread gate and per-row flag fire on the real number, not on a blank
                "median_half_spread_bps": round(float(statistics.median(extreme)), 6) if extreme else "",
                "max_half_spread_bps": round(max(extreme), 6) if extreme else "",
                "min_half_spread_bps": round(min(extreme), 6) if extreme else "",
                "spread_source": "ibkr_historical_bid_ask" if extreme else "",
                "spread_status": "failed",
                "spread_reason": reason,
            })
    return snapshot


def init_liquidity_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(LIQUIDITY_TABLES_SQL)
        _ensure_column(conn, "spread_snapshot", "latest_sample_date_et", "TEXT")
        _ensure_column(conn, "spread_snapshot", "latest_sample_age_days", "INTEGER")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _db_value(row: dict[str, Any], key: str) -> Any:
    value = row.get(key, "")
    if value == "":
        return None
    return value


def upsert_spread_samples(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    init_liquidity_tables(conn)
    now = utc_now()
    with conn:
        conn.executemany(
            """
            INSERT INTO ib_spread_samples(
                as_of_date, ticker, query_symbol, target_time_et, bar_date_et, bar_timestamp_et, bar_size,
                bid, ask, midpoint, spread_bps, half_spread_bps, source, status, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date, ticker, target_time_et) DO UPDATE SET
                query_symbol=excluded.query_symbol,
                bar_date_et=excluded.bar_date_et,
                bar_timestamp_et=excluded.bar_timestamp_et,
                bar_size=excluded.bar_size,
                bid=excluded.bid,
                ask=excluded.ask,
                midpoint=excluded.midpoint,
                spread_bps=excluded.spread_bps,
                half_spread_bps=excluded.half_spread_bps,
                source=excluded.source,
                status=excluded.status,
                reason=excluded.reason,
                created_at=excluded.created_at
            """,
            [
                (
                    row.get("as_of_date"),
                    str(row.get("ticker", "")).upper(),
                    _db_value(row, "query_symbol"),
                    row.get("target_time_et"),
                    _db_value(row, "bar_date_et"),
                    _db_value(row, "bar_timestamp_et"),
                    _db_value(row, "bar_size"),
                    _db_value(row, "bid"),
                    _db_value(row, "ask"),
                    _db_value(row, "midpoint"),
                    _db_value(row, "spread_bps"),
                    _db_value(row, "half_spread_bps"),
                    _db_value(row, "source"),
                    row.get("status"),
                    _db_value(row, "reason"),
                    now,
                )
                for row in rows
            ],
        )
    return len(rows)


def upsert_spread_snapshot(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    init_liquidity_tables(conn)
    now = utc_now()
    with conn:
        conn.executemany(
            """
            INSERT INTO spread_snapshot(
                as_of_date, ticker, requested_sample_count, valid_sample_count, latest_sample_date_et,
                latest_sample_age_days, median_half_spread_bps, max_half_spread_bps, min_half_spread_bps,
                spread_source, spread_status, spread_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date, ticker) DO UPDATE SET
                requested_sample_count=excluded.requested_sample_count,
                valid_sample_count=excluded.valid_sample_count,
                latest_sample_date_et=excluded.latest_sample_date_et,
                latest_sample_age_days=excluded.latest_sample_age_days,
                median_half_spread_bps=excluded.median_half_spread_bps,
                max_half_spread_bps=excluded.max_half_spread_bps,
                min_half_spread_bps=excluded.min_half_spread_bps,
                spread_source=excluded.spread_source,
                spread_status=excluded.spread_status,
                spread_reason=excluded.spread_reason,
                created_at=excluded.created_at
            """,
            [
                (
                    row.get("as_of_date"),
                    str(row.get("ticker", "")).upper(),
                    _db_value(row, "requested_sample_count"),
                    _db_value(row, "valid_sample_count"),
                    _db_value(row, "latest_sample_date_et"),
                    _db_value(row, "latest_sample_age_days"),
                    _db_value(row, "median_half_spread_bps"),
                    _db_value(row, "max_half_spread_bps"),
                    _db_value(row, "min_half_spread_bps"),
                    _db_value(row, "spread_source"),
                    row.get("spread_status"),
                    _db_value(row, "spread_reason"),
                    now,
                )
                for row in rows
            ],
        )
    return len(rows)


def upsert_spread_snapshot_run(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    metadata: dict[str, Any],
    samples_sha256: str,
    snapshot_sha256: str,
) -> None:
    init_liquidity_tables(conn)
    counts = metadata.get("counts", {}) if isinstance(metadata.get("counts"), dict) else {}
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO spread_snapshot_runs(
                as_of_date, provider, generated_at, universe_source, requested_tickers, ok_tickers,
                fallback_tickers, failed_tickers, sample_rows, snapshot_rows, samples_sha256,
                snapshot_sha256, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date) DO UPDATE SET
                provider=excluded.provider,
                generated_at=excluded.generated_at,
                universe_source=excluded.universe_source,
                requested_tickers=excluded.requested_tickers,
                ok_tickers=excluded.ok_tickers,
                fallback_tickers=excluded.fallback_tickers,
                failed_tickers=excluded.failed_tickers,
                sample_rows=excluded.sample_rows,
                snapshot_rows=excluded.snapshot_rows,
                samples_sha256=excluded.samples_sha256,
                snapshot_sha256=excluded.snapshot_sha256,
                metadata_json=excluded.metadata_json,
                created_at=excluded.created_at
            """,
            (
                as_of,
                metadata.get("provider"),
                metadata.get("generated_at"),
                metadata.get("universe_source"),
                counts.get("requested_tickers"),
                counts.get("ok_tickers"),
                counts.get("fallback_tickers"),
                counts.get("failed_tickers"),
                counts.get("sample_rows"),
                counts.get("snapshot_rows"),
                samples_sha256,
                snapshot_sha256,
                metadata.get("metadata_json", ""),
                now,
            ),
        )
