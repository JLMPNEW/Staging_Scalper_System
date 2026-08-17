from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from consumer_defensive.core.db import execute_schema_script, require_lastrowid, utc_now
from consumer_defensive.core.market_data import (
    NORGATE_SOURCE_ID,
    YAHOO_SOURCE_ID,
    upsert_corporate_actions,
    upsert_price_bars,
)
from consumer_defensive.core.universe import MODEL_FAMILY, normalize_ticker, read_yaml


TERMINAL_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fact_terminal_event_reconciliation (
    ticker TEXT PRIMARY KEY,
    security_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    economic_event_date TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    provider_last_quoted_date TEXT NOT NULL,
    terminal_type TEXT NOT NULL,
    cash_consideration REAL,
    cash_currency TEXT NOT NULL,
    successor_ticker TEXT,
    successor_share_ratio REAL,
    successor_security_type TEXT,
    successor_reference_date TEXT,
    successor_price_source_id TEXT,
    successor_provider_symbol TEXT,
    contingent_right_id TEXT,
    contingent_right_units REAL,
    contingent_max_cash REAL,
    contingent_status TEXT,
    fixed_terminal_value REAL,
    terminal_value_method TEXT NOT NULL,
    survivorship_complete INTEGER NOT NULL CHECK(survivorship_complete IN (0, 1)),
    calibration_eligible INTEGER NOT NULL CHECK(calibration_eligible IN (0, 1)),
    reconciliation_status TEXT NOT NULL,
    primary_source_url TEXT NOT NULL,
    secondary_source_url TEXT,
    source_document_date TEXT,
    notes TEXT,
    source_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_cd_terminal_event_status
    ON fact_terminal_event_reconciliation(reconciliation_status, calibration_eligible);
"""

LEDGER_COLUMNS = (
    "ticker",
    "event_type",
    "economic_event_date",
    "last_trade_date",
    "provider_last_quoted_date",
    "terminal_type",
    "cash_consideration",
    "cash_currency",
    "successor_ticker",
    "successor_share_ratio",
    "successor_security_type",
    "successor_reference_date",
    "successor_price_source_id",
    "successor_provider_symbol",
    "contingent_right_id",
    "contingent_right_units",
    "contingent_max_cash",
    "contingent_status",
    "fixed_terminal_value",
    "terminal_value_method",
    "survivorship_complete",
    "calibration_eligible",
    "reconciliation_status",
    "primary_source_url",
    "secondary_source_url",
    "source_document_date",
    "notes",
)
ALLOWED_TERMINAL_TYPES = {
    "cash",
    "cash_and_stock",
    "successor_security",
    "wipeout",
    "cash_and_contingent_right",
}
ALLOWED_SOURCE_IDS = {YAHOO_SOURCE_ID, NORGATE_SOURCE_ID}


@dataclass(frozen=True)
class TerminalEventPolicy:
    path: Path
    payload: dict[str, Any]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def ledger_path(self) -> Path:
        raw = Path(str(self.payload["ledger_path"])).expanduser()
        return raw.resolve() if raw.is_absolute() else (self.base_dir / raw).resolve()


@dataclass(frozen=True)
class TerminalEvent:
    ticker: str
    event_type: str
    economic_event_date: str
    last_trade_date: str
    provider_last_quoted_date: str
    terminal_type: str
    cash_consideration: float | None
    cash_currency: str
    successor_ticker: str
    successor_share_ratio: float | None
    successor_security_type: str
    successor_reference_date: str
    successor_price_source_id: str
    successor_provider_symbol: str
    contingent_right_id: str
    contingent_right_units: float | None
    contingent_max_cash: float | None
    contingent_status: str
    fixed_terminal_value: float | None
    terminal_value_method: str
    survivorship_complete: int
    calibration_eligible: int
    reconciliation_status: str
    primary_source_url: str
    secondary_source_url: str
    source_document_date: str
    notes: str


def ensure_terminal_event_schema(conn: sqlite3.Connection) -> None:
    execute_schema_script(conn, TERMINAL_EVENT_SCHEMA_SQL)


def _float(raw: Any) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite terminal-event numeric value: {raw!r}")
    return value


def _flag(raw: Any, *, field: str) -> int:
    text = str(raw).strip()
    if text not in {"0", "1"}:
        raise ValueError(f"{field} must be 0 or 1; got {raw!r}.")
    return int(text)


def _iso_date(raw: Any, *, field: str, required: bool = True) -> str:
    text = str(raw or "").strip()
    if not text and not required:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date; got {raw!r}.") from exc
    return text


def load_terminal_event_policy(path: Path) -> TerminalEventPolicy:
    resolved = path.expanduser().resolve()
    payload = read_yaml(resolved)
    if payload.get("policy_version") != "consumer_defensive_terminal_events_v1":
        raise ValueError("Terminal-event policy_version must be consumer_defensive_terminal_events_v1.")
    if payload.get("model_family") != MODEL_FAMILY:
        raise ValueError(f"Terminal-event model_family must be {MODEL_FAMILY!r}.")
    expected = [normalize_ticker(value) for value in payload.get("required_delisted_tickers") or []]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("Terminal-event required_delisted_tickers must be non-empty and unique.")
    successor = payload.get("successor_prices") or {}
    if set(successor.get("allowed_source_ids") or []) != ALLOWED_SOURCE_IDS:
        raise ValueError("Terminal-event successor price sources must be exactly Yahoo and Norgate.")
    if int(successor.get("minimum_required_rows_from_reference") or 0) != (
        int(successor.get("maximum_calibration_horizon_trading_days") or -1) + 1
    ):
        raise ValueError("Successor minimum rows must include the reference row plus the maximum horizon.")
    policy = TerminalEventPolicy(path=resolved, payload=payload)
    if not policy.ledger_path.exists():
        raise FileNotFoundError(f"Terminal-event ledger not found: {policy.ledger_path}")
    return policy


def _validate_event(event: TerminalEvent) -> None:
    event_date = date.fromisoformat(event.economic_event_date)
    last_trade = date.fromisoformat(event.last_trade_date)
    provider_last = date.fromisoformat(event.provider_last_quoted_date)
    if last_trade > provider_last:
        raise ValueError(f"{event.ticker}: last_trade_date exceeds provider_last_quoted_date.")
    if event.terminal_type not in ALLOWED_TERMINAL_TYPES:
        raise ValueError(f"{event.ticker}: unsupported terminal_type {event.terminal_type!r}.")
    if not event.primary_source_url.startswith("https://"):
        raise ValueError(f"{event.ticker}: primary_source_url must be HTTPS.")
    if event.secondary_source_url and not event.secondary_source_url.startswith("https://"):
        raise ValueError(f"{event.ticker}: secondary_source_url must be HTTPS.")
    if event.source_document_date:
        date.fromisoformat(event.source_document_date)
    if event.terminal_type == "cash":
        if event.cash_consideration is None or event.cash_consideration <= 0:
            raise ValueError(f"{event.ticker}: cash event requires positive cash consideration.")
        if event.fixed_terminal_value != event.cash_consideration:
            raise ValueError(f"{event.ticker}: fixed cash terminal value must equal cash consideration.")
    if event.terminal_type == "wipeout":
        if event.fixed_terminal_value != 0 or event.cash_consideration != 0:
            raise ValueError(f"{event.ticker}: wipeout must have zero cash and terminal value.")
    has_successor = event.terminal_type in {"cash_and_stock", "successor_security"}
    if has_successor:
        if not event.successor_ticker or not event.successor_reference_date:
            raise ValueError(f"{event.ticker}: successor event requires ticker and reference date.")
        if event.successor_share_ratio is None or event.successor_share_ratio <= 0:
            raise ValueError(f"{event.ticker}: successor event requires a positive share ratio.")
        if event.successor_price_source_id not in ALLOWED_SOURCE_IDS:
            raise ValueError(f"{event.ticker}: successor price source is not approved.")
        if not event.successor_provider_symbol:
            raise ValueError(f"{event.ticker}: successor provider symbol is required.")
        if date.fromisoformat(event.successor_reference_date) < event_date:
            raise ValueError(f"{event.ticker}: successor reference date precedes the economic event.")
    elif any(
        (
            event.successor_ticker,
            event.successor_share_ratio,
            event.successor_reference_date,
            event.successor_price_source_id,
            event.successor_provider_symbol,
        )
    ):
        raise ValueError(f"{event.ticker}: non-successor event contains successor fields.")
    if event.terminal_type == "cash_and_contingent_right":
        if not event.contingent_right_id or event.contingent_status != "unresolved":
            raise ValueError(f"{event.ticker}: contingent-right event must identify an unresolved right.")
        if event.survivorship_complete or event.calibration_eligible:
            raise ValueError(f"{event.ticker}: unresolved contingent right cannot be calibration eligible.")
    if event.survivorship_complete != event.calibration_eligible:
        raise ValueError(f"{event.ticker}: complete and eligible flags must agree in the reviewed ledger.")


def load_terminal_event_ledger(policy: TerminalEventPolicy) -> list[TerminalEvent]:
    with policy.ledger_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != LEDGER_COLUMNS:
            raise ValueError(
                f"Terminal-event ledger columns do not match the contract; observed={reader.fieldnames}."
            )
        raw_rows = list(reader)
    events: list[TerminalEvent] = []
    seen: set[str] = set()
    for row_number, row in enumerate(raw_rows, start=2):
        ticker = normalize_ticker(row["ticker"])
        if not ticker or ticker in seen:
            raise ValueError(f"Terminal-event row {row_number} has a blank or duplicate ticker {ticker!r}.")
        seen.add(ticker)
        event = TerminalEvent(
            ticker=ticker,
            event_type=str(row["event_type"]).strip(),
            economic_event_date=_iso_date(row["economic_event_date"], field="economic_event_date"),
            last_trade_date=_iso_date(row["last_trade_date"], field="last_trade_date"),
            provider_last_quoted_date=_iso_date(row["provider_last_quoted_date"], field="provider_last_quoted_date"),
            terminal_type=str(row["terminal_type"]).strip(),
            cash_consideration=_float(row["cash_consideration"]),
            cash_currency=str(row["cash_currency"]).strip().upper(),
            successor_ticker=normalize_ticker(row["successor_ticker"]),
            successor_share_ratio=_float(row["successor_share_ratio"]),
            successor_security_type=str(row["successor_security_type"]).strip(),
            successor_reference_date=_iso_date(row["successor_reference_date"], field="successor_reference_date", required=False),
            successor_price_source_id=str(row["successor_price_source_id"]).strip(),
            successor_provider_symbol=str(row["successor_provider_symbol"]).strip().upper(),
            contingent_right_id=str(row["contingent_right_id"]).strip(),
            contingent_right_units=_float(row["contingent_right_units"]),
            contingent_max_cash=_float(row["contingent_max_cash"]),
            contingent_status=str(row["contingent_status"]).strip(),
            fixed_terminal_value=_float(row["fixed_terminal_value"]),
            terminal_value_method=str(row["terminal_value_method"]).strip(),
            survivorship_complete=_flag(row["survivorship_complete"], field="survivorship_complete"),
            calibration_eligible=_flag(row["calibration_eligible"], field="calibration_eligible"),
            reconciliation_status=str(row["reconciliation_status"]).strip(),
            primary_source_url=str(row["primary_source_url"]).strip(),
            secondary_source_url=str(row["secondary_source_url"]).strip(),
            source_document_date=_iso_date(row["source_document_date"], field="source_document_date", required=False),
            notes=str(row["notes"]).strip(),
        )
        _validate_event(event)
        events.append(event)
    expected = {normalize_ticker(value) for value in policy.payload["required_delisted_tickers"]}
    observed = {event.ticker for event in events}
    if observed != expected:
        raise ValueError(
            f"Terminal-event ledger ticker set mismatch; missing={sorted(expected-observed)} extra={sorted(observed-expected)}."
        )
    exclusions = {
        normalize_ticker(value)
        for value in (policy.payload.get("contingent_consideration") or {}).get("explicit_exclusions") or []
    }
    pending = {event.ticker for event in events if not event.calibration_eligible}
    if pending != exclusions:
        raise ValueError(f"Terminal-event explicit exclusions do not match pending rows: {sorted(pending)}.")
    return events


def _stock_terms(event: TerminalEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {"terminal_type": event.terminal_type}
    if event.successor_ticker:
        payload["successor"] = {
            "ticker": event.successor_ticker,
            "share_ratio": event.successor_share_ratio,
            "security_type": event.successor_security_type,
            "reference_date": event.successor_reference_date,
            "price_source_id": event.successor_price_source_id,
            "provider_symbol": event.successor_provider_symbol,
        }
    if event.contingent_right_id:
        payload["contingent_right"] = {
            "right_id": event.contingent_right_id,
            "units": event.contingent_right_units,
            "maximum_cash": event.contingent_max_cash,
            "status": event.contingent_status,
        }
    return payload


def reconcile_terminal_events(
    conn: sqlite3.Connection,
    policy: TerminalEventPolicy,
) -> dict[str, Any]:
    ensure_terminal_event_schema(conn)
    events = load_terminal_event_ledger(policy)
    source_id = str(policy.payload["source_id"])
    if conn.execute("SELECT 1 FROM source_registry WHERE source_id=?", (source_id,)).fetchone() is None:
        raise RuntimeError(f"Terminal-event source_id is not registered: {source_id}")
    expected = {event.ticker for event in events}
    loaded = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT s.ticker FROM dim_security s
            JOIN dim_company c ON c.company_id=s.company_id
            WHERE s.listing_status='delisted' AND c.is_active=0
            """
        ).fetchall()
    }
    if loaded != expected:
        raise RuntimeError(
            f"Loaded delisted securities do not match the terminal ledger; missing={sorted(expected-loaded)} extra={sorted(loaded-expected)}."
        )
    now = utc_now()
    reviewed_at = str(policy.payload["reviewed_at"])
    with conn:
        for event in events:
            security = conn.execute(
                "SELECT security_id, company_id FROM dim_security WHERE ticker=? AND listing_status='delisted'",
                (event.ticker,),
            ).fetchone()
            if security is None:
                raise RuntimeError(f"Terminal-event security is missing: {event.ticker}")
            security_id, company_id = int(security[0]), int(security[1])
            values = (
                event.ticker,
                security_id,
                event.event_type,
                event.economic_event_date,
                event.last_trade_date,
                event.provider_last_quoted_date,
                event.terminal_type,
                event.cash_consideration,
                event.cash_currency,
                event.successor_ticker or None,
                event.successor_share_ratio,
                event.successor_security_type or None,
                event.successor_reference_date or None,
                event.successor_price_source_id or None,
                event.successor_provider_symbol or None,
                event.contingent_right_id or None,
                event.contingent_right_units,
                event.contingent_max_cash,
                event.contingent_status or None,
                event.fixed_terminal_value,
                event.terminal_value_method,
                event.survivorship_complete,
                event.calibration_eligible,
                event.reconciliation_status,
                event.primary_source_url,
                event.secondary_source_url or None,
                event.source_document_date or None,
                event.notes,
                source_id,
                reviewed_at,
                now,
                now,
            )
            conn.execute(
                """
                INSERT INTO fact_terminal_event_reconciliation(
                    ticker, security_id, event_type, economic_event_date, last_trade_date,
                    provider_last_quoted_date, terminal_type, cash_consideration, cash_currency,
                    successor_ticker, successor_share_ratio, successor_security_type,
                    successor_reference_date, successor_price_source_id, successor_provider_symbol,
                    contingent_right_id, contingent_right_units, contingent_max_cash, contingent_status,
                    fixed_terminal_value, terminal_value_method, survivorship_complete,
                    calibration_eligible, reconciliation_status, primary_source_url,
                    secondary_source_url, source_document_date, notes, source_id, reviewed_at,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET
                    security_id=excluded.security_id, event_type=excluded.event_type,
                    economic_event_date=excluded.economic_event_date,
                    last_trade_date=excluded.last_trade_date,
                    provider_last_quoted_date=excluded.provider_last_quoted_date,
                    terminal_type=excluded.terminal_type,
                    cash_consideration=excluded.cash_consideration,
                    cash_currency=excluded.cash_currency,
                    successor_ticker=excluded.successor_ticker,
                    successor_share_ratio=excluded.successor_share_ratio,
                    successor_security_type=excluded.successor_security_type,
                    successor_reference_date=excluded.successor_reference_date,
                    successor_price_source_id=excluded.successor_price_source_id,
                    successor_provider_symbol=excluded.successor_provider_symbol,
                    contingent_right_id=excluded.contingent_right_id,
                    contingent_right_units=excluded.contingent_right_units,
                    contingent_max_cash=excluded.contingent_max_cash,
                    contingent_status=excluded.contingent_status,
                    fixed_terminal_value=excluded.fixed_terminal_value,
                    terminal_value_method=excluded.terminal_value_method,
                    survivorship_complete=excluded.survivorship_complete,
                    calibration_eligible=excluded.calibration_eligible,
                    reconciliation_status=excluded.reconciliation_status,
                    primary_source_url=excluded.primary_source_url,
                    secondary_source_url=excluded.secondary_source_url,
                    source_document_date=excluded.source_document_date,
                    notes=excluded.notes, source_id=excluded.source_id,
                    reviewed_at=excluded.reviewed_at, updated_at=excluded.updated_at
                """,
                values,
            )
            conn.execute(
                """
                UPDATE dim_universe_membership
                SET historical_calibration_eligible_flag=?, updated_at=?
                WHERE security_id=? AND model_family=?
                """,
                (event.calibration_eligible, now, security_id, MODEL_FAMILY),
            )
            conn.execute(
                "DELETE FROM fact_security_event WHERE ticker=? AND source_id=?",
                (event.ticker, source_id),
            )
            conn.execute(
                """
                INSERT INTO fact_security_event(
                    security_id, ticker, event_type, event_date, last_trade_date,
                    successor_ticker, cash_consideration, stock_consideration_json,
                    terminal_value, terminal_value_currency, survivorship_complete,
                    source_id, source_detail, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    security_id,
                    event.ticker,
                    event.event_type,
                    event.economic_event_date,
                    event.last_trade_date,
                    event.successor_ticker or None,
                    event.cash_consideration,
                    json.dumps(_stock_terms(event), sort_keys=True),
                    event.fixed_terminal_value,
                    event.cash_currency,
                    event.survivorship_complete,
                    source_id,
                    json.dumps(
                        {
                            "primary_source_url": event.primary_source_url,
                            "secondary_source_url": event.secondary_source_url,
                            "provider_last_quoted_date": event.provider_last_quoted_date,
                            "terminal_value_method": event.terminal_value_method,
                            "calibration_eligible": event.calibration_eligible,
                            "notes": event.notes,
                        },
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE dim_company SET data_quality_status=?, updated_at=? WHERE company_id=?",
                (
                    "terminal_event_reconciled"
                    if event.survivorship_complete
                    else "terminal_contingent_value_pending",
                    now,
                    company_id,
                ),
            )
    return {
        "events_loaded": len(events),
        "survivorship_complete": sum(event.survivorship_complete for event in events),
        "calibration_eligible": sum(event.calibration_eligible for event in events),
        "explicitly_excluded": sorted(event.ticker for event in events if not event.calibration_eligible),
    }


def _terminal_row(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fact_terminal_event_reconciliation WHERE ticker=?",
        (normalize_ticker(ticker),),
    ).fetchone()


def terminal_horizon_value(
    conn: sqlite3.Connection,
    policy: TerminalEventPolicy,
    *,
    ticker: str,
    horizon_date: str,
) -> dict[str, Any]:
    target = _iso_date(horizon_date, field="horizon_date")
    row = _terminal_row(conn, ticker)
    if row is None:
        return {"ticker": normalize_ticker(ticker), "horizon_date": target, "calculation_status": "terminal_event_missing", "terminal_value": None}
    result: dict[str, Any] = {
        "ticker": str(row["ticker"]),
        "horizon_date": target,
        "economic_event_date": str(row["economic_event_date"]),
        "terminal_type": str(row["terminal_type"]),
        "currency": str(row["cash_currency"]),
        "terminal_value": None,
        "cash_component": float(row["cash_consideration"] or 0.0),
        "stock_component": None,
        "successor_ticker": str(row["successor_ticker"] or ""),
        "successor_source_id": str(row["successor_price_source_id"] or ""),
        "successor_reference_price_date": "",
        "successor_horizon_price_date": "",
        "survivorship_complete": int(row["survivorship_complete"]),
        "calibration_eligible": int(row["calibration_eligible"]),
    }
    if target < str(row["economic_event_date"]):
        result["survivorship_complete"] = 1
        result["calibration_eligible"] = 1
        result["calculation_status"] = "pre_terminal_event"
        return result
    terminal_type = str(row["terminal_type"])
    if terminal_type == "cash_and_contingent_right":
        result["terminal_value"] = float(row["fixed_terminal_value"])
        result["calculation_status"] = "contingent_value_unresolved"
        return result
    if terminal_type in {"cash", "wipeout"}:
        result["terminal_value"] = float(row["fixed_terminal_value"])
        result["calculation_status"] = "resolved_fixed_terminal_value"
        return result
    reference_date = str(row["successor_reference_date"])
    source_id = str(row["successor_price_source_id"])
    max_lag = int(policy.payload["successor_prices"]["max_reference_lag_calendar_days"])
    reference_limit = (date.fromisoformat(reference_date) + timedelta(days=max_lag)).isoformat()
    reference = conn.execute(
        """
        SELECT bar_date, close, adjusted_close FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?
          AND close>0 AND adjusted_close>0
        ORDER BY bar_date LIMIT 1
        """,
        (str(row["successor_ticker"]), source_id, reference_date, reference_limit),
    ).fetchone()
    if reference is None:
        result["calculation_status"] = "successor_reference_price_missing"
        return result
    if target < str(reference["bar_date"]):
        result["calculation_status"] = "successor_not_yet_trading"
        return result
    horizon = conn.execute(
        """
        SELECT bar_date, adjusted_close FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?
          AND adjusted_close>0
        ORDER BY bar_date DESC LIMIT 1
        """,
        (str(row["successor_ticker"]), source_id, str(reference["bar_date"]), target),
    ).fetchone()
    if horizon is None:
        result["calculation_status"] = "successor_horizon_price_missing"
        return result
    ratio = float(row["successor_share_ratio"])
    stock_component = ratio * float(reference["close"]) * (
        float(horizon["adjusted_close"]) / float(reference["adjusted_close"])
    )
    result.update(
        {
            "stock_component": stock_component,
            "terminal_value": float(row["cash_consideration"] or 0.0) + stock_component,
            "successor_reference_price_date": str(reference["bar_date"]),
            "successor_horizon_price_date": str(horizon["bar_date"]),
            "calculation_status": "resolved_cash_and_or_successor_total_return",
        }
    )
    return result


def _successor_events_as_of(
    events: Iterable[TerminalEvent],
    *,
    source_id: str,
    as_of: str | None,
) -> list[TerminalEvent]:
    if as_of is not None:
        date.fromisoformat(as_of)
    return [
        event for event in events
        if event.successor_price_source_id == source_id
        and (
            as_of is None
            or (event.economic_event_date <= as_of and event.successor_reference_date <= as_of)
        )
    ]


def norgate_successor_events(events: Iterable[TerminalEvent], *, as_of: str | None = None) -> list[TerminalEvent]:
    return _successor_events_as_of(events, source_id=NORGATE_SOURCE_ID, as_of=as_of)


def yahoo_successor_tickers(events: Iterable[TerminalEvent], *, as_of: str | None = None) -> list[str]:
    return sorted({event.successor_ticker for event in _successor_events_as_of(events, source_id=YAHOO_SOURCE_ID, as_of=as_of)})


def load_norgate_successor_prices(
    conn: sqlite3.Connection,
    events: Iterable[TerminalEvent],
    *,
    provider: Any,
    end: str,
) -> dict[str, Any]:
    from consumer_defensive.core.norgate_prices import fetch_norgate_prices
    from consumer_defensive.core.norgate_runtime import (
        NORGATE_EQUITY_DATABASES,
        NorgateSnapshotChanged,
        norgate_database_fingerprint,
        require_norgate_snapshot,
    )

    requested = [
        event
        for event in norgate_successor_events(events, as_of=end)
        if event.successor_reference_date <= end
    ]
    provider_fingerprint_start = norgate_database_fingerprint(
        provider,
        NORGATE_EQUITY_DATABASES,
    )
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (NORGATE_SOURCE_ID, now, now),
    )
    run_id = require_lastrowid(cursor, context="create Norgate successor ingestion run")
    failures: list[dict[str, str]] = []
    bars_written = 0
    actions_written = 0
    staged_results: list[tuple[TerminalEvent, Any]] = []

    def fail_provider_change(exc: NorgateSnapshotChanged) -> None:
        message = {
            "error": "norgate_provider_changed_midrun",
            "changed_databases": list(exc.changed_databases),
            "provider_updated_at_start": provider_fingerprint_start,
            "provider_updated_at_end": exc.observed,
            "context": exc.context,
        }
        with conn:
            conn.execute(
                """UPDATE ingestion_runs
                   SET completed_at=?,status='failed',request_count=?,row_count=0,
                       message=? WHERE ingestion_run_id=?""",
                (
                    utc_now(),
                    len(staged_results) * 2,
                    json.dumps(message, sort_keys=True),
                    run_id,
                ),
            )
        raise RuntimeError(str(exc)) from exc

    def fence(context: str) -> dict[str, str]:
        try:
            return require_norgate_snapshot(
                provider,
                provider_fingerprint_start,
                context=context,
            )
        except NorgateSnapshotChanged as exc:
            fail_provider_change(exc)
            raise AssertionError("unreachable")

    for event in requested:
        result = fetch_norgate_prices(
            provider,
            ticker=event.successor_ticker,
            symbol=event.successor_provider_symbol,
            listing_status="terminal_successor_history",
            start=event.successor_reference_date,
            end=end,
        )
        staged_results.append((event, result))
        fence("during terminal-successor price extraction")
    provider_fingerprint_end = fence("before terminal-successor price publication")

    with conn:
        for event, result in staged_results:
            if result.error:
                failures.append({"ticker": event.successor_ticker, "symbol": event.successor_provider_symbol, "error": result.error})
                continue
            conn.execute(
                "DELETE FROM fact_price_ohlcv WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?",
                (event.successor_ticker, NORGATE_SOURCE_ID, event.successor_reference_date, end),
            )
            bars_written += upsert_price_bars(conn, result.bars)
            actions_written += upsert_corporate_actions(conn, result.actions)
        conn.execute(
            """
            UPDATE ingestion_runs SET completed_at=?, status=?, request_count=?, row_count=?, message=?
            WHERE ingestion_run_id=?
            """,
            (
                utc_now(),
                "success" if not failures else "failed",
                len(requested) * 2,
                bars_written,
                json.dumps({"terminal_successor_failures": failures}, sort_keys=True),
                run_id,
            ),
        )
    return {
        "source_id": NORGATE_SOURCE_ID,
        "tickers_requested": len(requested),
        "bars_written": bars_written,
        "actions_written": actions_written,
        "provider_database_updated_at_start": provider_fingerprint_start,
        "provider_database_updated_at_end": provider_fingerprint_end,
        "failures": failures,
    }


def validate_terminal_events(
    conn: sqlite3.Connection,
    policy: TerminalEventPolicy,
    *,
    as_of: str,
) -> dict[str, Any]:
    date.fromisoformat(as_of)
    expected = {normalize_ticker(value) for value in policy.payload["required_delisted_tickers"]}
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, *, severity: str = "error") -> None:
        checks.append({"check": name, "status": "PASS" if passed else ("WARN" if severity == "warning" else "FAIL"), "detail": detail})
        if not passed:
            (warnings if severity == "warning" else errors).append(f"{name}: {detail}")

    rows = {
        str(row["ticker"]): row
        for row in conn.execute("SELECT * FROM fact_terminal_event_reconciliation ORDER BY ticker").fetchall()
    }
    check(
        "terminal_ledger_exact_coverage",
        set(rows) == expected,
        f"observed={len(rows)} expected={len(expected)} missing={sorted(expected-set(rows))} extra={sorted(set(rows)-expected)}",
    )
    provider_mismatch: list[str] = []
    provider_deferred: list[str] = []
    for ticker, row in rows.items():
        if str(row["economic_event_date"]) > as_of:
            continue
        expected_last = str(row["provider_last_quoted_date"])
        if expected_last > as_of:
            provider_deferred.append(
                f'{ticker}:provider_terminal_date={expected_last}:as_of={as_of}'
            )
            continue
        observed = conn.execute(
            '''SELECT MAX(bar_date) FROM fact_price_ohlcv
               WHERE ticker=? AND source_id=? AND bar_date<=?''',
            (ticker, NORGATE_SOURCE_ID, as_of),
        ).fetchone()[0]
        if str(observed or "") != expected_last:
            provider_mismatch.append(f"{ticker}:observed={observed or ''}:expected={expected_last}")
    check("provider_last_quote_reconciled", not provider_mismatch, f"mismatches={provider_mismatch}")
    if provider_deferred:
        check(
            'provider_last_quote_not_yet_observable',
            False,
            f'deferred={provider_deferred}',
            severity='warning',
        )

    pending = sorted(ticker for ticker, row in rows.items() if not int(row["calibration_eligible"]))
    exclusions = sorted(
        normalize_ticker(value)
        for value in (policy.payload.get("contingent_consideration") or {}).get("explicit_exclusions") or []
    )
    check("explicit_terminal_exclusions", pending == exclusions, f"pending={pending} configured={exclusions}")
    membership_mismatches: list[str] = []
    for ticker, row in rows.items():
        flags = {
            int(value[0])
            for value in conn.execute(
                """
                SELECT DISTINCT d.historical_calibration_eligible_flag
                FROM dim_universe_membership d
                JOIN dim_security s ON s.security_id=d.security_id
                WHERE s.ticker=? AND d.model_family=?
                """,
                (ticker, MODEL_FAMILY),
            ).fetchall()
        }
        expected_flag = int(row["calibration_eligible"])
        if flags and flags != {expected_flag}:
            membership_mismatches.append(f"{ticker}:membership={sorted(flags)}:terminal={expected_flag}")
    check(
        "membership_terminal_calibration_eligibility_consistent",
        not membership_mismatches,
        f"mismatches={membership_mismatches}",
    )
    effective_pending = sorted(
        ticker for ticker in pending
        if ticker in rows and str(rows[ticker]["economic_event_date"]) <= as_of
    )
    check(
        "unresolved_contingent_consideration",
        not effective_pending,
        f"terminal_crossing_labels_excluded={effective_pending}; fixed cash floor is stored but contingent value is not assumed",
        severity="warning",
    )

    required_rows = int(policy.payload["successor_prices"]["minimum_required_rows_from_reference"])
    successor_failures: list[str] = []
    successor_deferred: list[str] = []
    resolved_samples: dict[str, float] = {}
    for ticker, row in rows.items():
        if not row["successor_ticker"] or str(row["economic_event_date"]) > as_of:
            continue
        price_rows = conn.execute(
            """
            SELECT bar_date FROM fact_price_ohlcv
            WHERE ticker=? AND source_id=? AND bar_date>=? AND bar_date<=?
              AND close>0 AND adjusted_close>0
            ORDER BY bar_date LIMIT ?
            """,
            (
                str(row["successor_ticker"]),
                str(row["successor_price_source_id"]),
                str(row["successor_reference_date"]),
                as_of,
                required_rows,
            ),
        ).fetchall()
        if len(price_rows) < required_rows:
            calendar_rows = int(
                conn.execute(
                    '''SELECT COUNT(*) FROM fact_price_ohlcv
                       WHERE ticker='SPY' AND source_id=?
                         AND bar_date>=? AND bar_date<=?
                         AND close>0 AND adjusted_close>0''',
                    (
                        YAHOO_SOURCE_ID,
                        str(row['successor_reference_date']),
                        as_of,
                    ),
                ).fetchone()[0]
            )
            detail = (
                f"{ticker}:{row['successor_ticker']}:rows={len(price_rows)}:"
                f'calendar_rows={calendar_rows}'
            )
            if calendar_rows >= required_rows:
                successor_failures.append(detail)
            else:
                successor_deferred.append(detail)
            continue
        sample_horizon = str(price_rows[-1]["bar_date"])
        outcome = terminal_horizon_value(conn, policy, ticker=ticker, horizon_date=sample_horizon)
        if not str(outcome.get("calculation_status", "")).startswith("resolved_"):
            successor_failures.append(f"{ticker}:{outcome.get('calculation_status')}")
            continue
        resolved_samples[ticker] = float(outcome["terminal_value"])
    check(
        "successor_total_return_horizon_coverage",
        not successor_failures,
        f"required_rows={required_rows} failures={successor_failures}",
    )
    if successor_deferred:
        check(
            'successor_horizon_not_yet_observable',
            False,
            f'required_rows={required_rows} deferred={successor_deferred}',
            severity='warning',
        )

    df = rows.get("DF")
    check(
        "df_economic_terminal_overrides_later_quotes",
        bool(
            df
            and str(df["economic_event_date"]) == "2021-05-28"
            and str(df["provider_last_quoted_date"]) == "2021-06-02"
            and float(df["fixed_terminal_value"]) == 0.0
        ),
        "economic_event=2021-05-28 provider_last_quote=2021-06-02 terminal_value=0",
    )
    complete = sum(int(row["survivorship_complete"]) for row in rows.values())
    eligible = sum(int(row["calibration_eligible"]) for row in rows.values())
    return {
        "status": "PASS" if not errors else "FAIL",
        "reconciliation_state": "PASS_WITH_EXCLUSION" if not errors and effective_pending else ("PASS" if not errors else "FAIL"),
        "as_of": as_of,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "resolved_successor_samples": resolved_samples,
        "counts": {
            "events": len(rows),
            "survivorship_complete": complete,
            "calibration_eligible": eligible,
            "explicitly_excluded": len(pending),
        },
    }
