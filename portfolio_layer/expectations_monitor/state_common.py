from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_items (
    item_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_uid TEXT NOT NULL,
    ticker_hint TEXT NOT NULL,
    published_at_utc TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','classified','duplicate','irrelevant','error')),
    UNIQUE(source, source_uid)
);

CREATE TABLE IF NOT EXISTS event_taxonomy (
    event_type TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    default_severity REAL NOT NULL,
    default_credibility REAL NOT NULL,
    default_half_life_td INTEGER,
    decay_mode TEXT NOT NULL CHECK (decay_mode IN ('half_life','until_replaced','recompute_daily')),
    thesis_break_eligible INTEGER NOT NULL CHECK (thesis_break_eligible IN (0,1)),
    taxonomy_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_type TEXT NOT NULL REFERENCES event_taxonomy(event_type),
    category TEXT NOT NULL,
    event_date TEXT NOT NULL,
    detected_at_utc TEXT NOT NULL,
    direction REAL NOT NULL CHECK (direction BETWEEN -1 AND 1),
    severity REAL NOT NULL CHECK (severity >= 0),
    credibility REAL NOT NULL CHECK (credibility BETWEEN 0 AND 1),
    novelty REAL NOT NULL CHECK (novelty BETWEEN 0 AND 1),
    relevance REAL NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    impact_0 REAL NOT NULL,
    half_life_td INTEGER,
    decay_mode TEXT NOT NULL,
    driver_tag TEXT NOT NULL DEFAULT '',
    origin_ticker TEXT NOT NULL DEFAULT '',
    source_item_ids TEXT NOT NULL,
    classifier TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    rationale_text TEXT NOT NULL,
    material_flag INTEGER NOT NULL CHECK (material_flag IN (0,1)),
    thesis_break_flag INTEGER NOT NULL CHECK (thesis_break_flag IN (0,1)),
    review_status TEXT NOT NULL CHECK (review_status IN ('auto','pending_review','confirmed','dismissed'))
);

CREATE INDEX IF NOT EXISTS ix_monitor_events_ticker_date
ON events(ticker, event_date, event_type);

CREATE TABLE IF NOT EXISTS market_signals_daily (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    benchmark_ticker TEXT NOT NULL,
    market_data_status TEXT NOT NULL DEFAULT 'current' CHECK (market_data_status IN ('current','missing_latest')),
    abnormal_ret_1d_z REAL,
    rel_ret_5d REAL,
    rel_ret_20d REAL,
    volume_z REAL,
    realized_vol_ratio REAL,
    below_ma50 INTEGER NOT NULL CHECK (below_ma50 IN (0,1)),
    below_ma200 INTEGER NOT NULL CHECK (below_ma200 IN (0,1)),
    new_52w_low INTEGER NOT NULL CHECK (new_52w_low IN (0,1)),
    gap_state TEXT NOT NULL,
    market_component_points REAL NOT NULL,
    input_manifest_sha256 TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    PRIMARY KEY (ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS les_snapshots (
    ticker TEXT NOT NULL,
    asof_ts TEXT NOT NULL,
    run_as_of TEXT NOT NULL,
    baseline_points REAL NOT NULL,
    company_event_points REAL NOT NULL,
    external_intel_points REAL NOT NULL,
    market_points REAL NOT NULL,
    peer_readthrough_points REAL NOT NULL,
    les_total REAL NOT NULL,
    internal_state TEXT NOT NULL CHECK (internal_state IN ('green','stable','watch','deteriorating','broken')),
    action_state TEXT NOT NULL CHECK (action_state IN ('buy_candidate','add_candidate','hold','watch','deteriorating','suspend_adds','exit_review')),
    market_data_status TEXT NOT NULL DEFAULT 'current' CHECK (market_data_status IN ('current','missing_latest')),
    prior_internal_state TEXT NOT NULL DEFAULT '',
    state_changed INTEGER NOT NULL CHECK (state_changed IN (0,1)),
    escalation_flags_json TEXT NOT NULL,
    top_contributors_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    PRIMARY KEY (ticker, run_as_of)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    transition_ts TEXT NOT NULL,
    run_as_of TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger TEXT NOT NULL,
    rule_id TEXT NOT NULL DEFAULT '',
    evidence_event_ids TEXT NOT NULL,
    dwell_days_met INTEGER NOT NULL,
    approved_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(ticker, run_as_of)
);

CREATE TABLE IF NOT EXISTS monitor_state_outcome_ledger (
    row_sequence INTEGER PRIMARY KEY CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    published_as_of TEXT NOT NULL,
    published_at_utc TEXT NOT NULL,
    action_state TEXT NOT NULL,
    internal_state TEXT NOT NULL,
    les_total REAL NOT NULL,
    market_price_at_publish REAL,
    source_manifest_sha256 TEXT NOT NULL,
    resolution_json TEXT NOT NULL DEFAULT '',
    resolution_available_at_utc TEXT NOT NULL DEFAULT '',
    UNIQUE(ticker, published_as_of)
);

CREATE TRIGGER IF NOT EXISTS monitor_state_outcome_no_delete
BEFORE DELETE ON monitor_state_outcome_ledger BEGIN
    SELECT RAISE(ABORT, 'monitor state outcome ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS monitor_state_outcome_no_update
BEFORE UPDATE ON monitor_state_outcome_ledger BEGIN
    SELECT RAISE(ABORT, 'monitor state outcome ledger is append-only');
END;

CREATE TABLE IF NOT EXISTS monitor_state_resolution_ledger (
    row_sequence INTEGER PRIMARY KEY CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    publication_row_sha256 TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    published_as_of TEXT NOT NULL,
    resolved_through TEXT NOT NULL,
    forward_returns_json TEXT NOT NULL,
    sector_excess_returns_json TEXT NOT NULL,
    maximum_favorable_excursion REAL NOT NULL,
    maximum_adverse_excursion REAL NOT NULL,
    state_changes_json TEXT NOT NULL,
    event_occurrences_json TEXT NOT NULL,
    resolution_available_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_state_publication_source_aliases (
    publication_row_sha256 TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    PRIMARY KEY (publication_row_sha256, source_manifest_sha256)
);
CREATE TRIGGER IF NOT EXISTS monitor_state_publication_alias_no_delete
BEFORE DELETE ON monitor_state_publication_source_aliases BEGIN
    SELECT RAISE(ABORT, 'monitor state publication aliases are append-only');
END;
CREATE TRIGGER IF NOT EXISTS monitor_state_publication_alias_no_update
BEFORE UPDATE ON monitor_state_publication_source_aliases BEGIN
    SELECT RAISE(ABORT, 'monitor state publication aliases are append-only');
END;

CREATE TRIGGER IF NOT EXISTS monitor_state_resolution_no_delete
BEFORE DELETE ON monitor_state_resolution_ledger BEGIN
    SELECT RAISE(ABORT, 'monitor state resolution ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS monitor_state_resolution_no_update
BEFORE UPDATE ON monitor_state_resolution_ledger BEGIN
    SELECT RAISE(ABORT, 'monitor state resolution ledger is append-only');
END;
"""

TAXONOMY_VERSION = "expectations_event_taxonomy_v1"
CLASSIFIER_VERSION = "structured_monitor_classifier_v1"
INTERNAL_STATES = ("green", "stable", "watch", "deteriorating", "broken")
ACTION_STATES = (
    "buy_candidate",
    "add_candidate",
    "hold",
    "watch",
    "deteriorating",
    "suspend_adds",
    "exit_review",
)


@dataclass(frozen=True)
class EventSpec:
    category: str
    severity: float
    credibility: float
    half_life_td: int | None
    decay_mode: str
    thesis_break_eligible: bool = False


EVENT_SPECS: dict[str, EventSpec] = {
    "guidance_cut": EventSpec("company_filing", 4.5, 1.00, None, "until_replaced", True),
    "guidance_raise": EventSpec("company_filing", 3.5, 1.00, None, "until_replaced"),
    "guidance_affirmed": EventSpec("company_filing", 1.5, 1.00, None, "until_replaced"),
    "preannounce_negative": EventSpec("company_filing", 4.5, 1.00, None, "until_replaced", True),
    "preannounce_positive": EventSpec("company_filing", 3.5, 1.00, None, "until_replaced"),
    "earnings_miss": EventSpec("company_announcement", 3.0, 1.00, 20, "half_life"),
    "earnings_beat": EventSpec("company_announcement", 2.5, 1.00, 20, "half_life"),
    "customer_loss_or_contract_cancellation": EventSpec("company_announcement", 4.5, 0.95, 120, "half_life", True),
    "customer_win_or_major_contract": EventSpec("company_announcement", 3.0, 0.95, 60, "half_life"),
    "executive_departure_ceo_cfo": EventSpec("company_filing", 3.0, 1.00, 120, "half_life", True),
    "product_delay_or_recall": EventSpec("company_announcement", 3.0, 0.95, 60, "half_life", True),
    "regulatory_action_adverse": EventSpec("company_filing", 4.0, 1.00, 90, "half_life", True),
    "regulatory_clearance_positive": EventSpec("company_filing", 3.0, 1.00, 60, "half_life"),
    "accounting_restatement_or_material_weakness": EventSpec("company_filing", 5.0, 1.00, 180, "half_life", True),
    "auditor_change_adverse": EventSpec("company_filing", 4.0, 1.00, 120, "half_life", True),
    "balance_sheet_distress": EventSpec("company_filing", 5.0, 1.00, 180, "half_life", True),
    "financing_dilutive": EventSpec("company_filing", 2.5, 1.00, 40, "half_life"),
    "insider_buy_cluster": EventSpec("company_filing", 1.5, 0.90, 30, "half_life"),
    "insider_sale_cluster": EventSpec("company_filing", 1.5, 0.90, 30, "half_life"),
    "analyst_downgrade": EventSpec("external_intel", 2.0, 0.75, 15, "half_life"),
    "analyst_upgrade": EventSpec("external_intel", 2.0, 0.75, 15, "half_life"),
    "estimate_revision_down": EventSpec("external_intel", 2.5, 0.75, 30, "half_life"),
    "estimate_revision_up": EventSpec("external_intel", 2.5, 0.75, 30, "half_life"),
    "channel_check_negative": EventSpec("external_intel", 3.5, 0.70, 30, "half_life"),
    "channel_check_positive": EventSpec("external_intel", 3.5, 0.70, 30, "half_life"),
    "churn_or_pricing_pressure_report": EventSpec("external_intel", 4.5, 0.65, 90, "half_life", True),
    "abnormal_return_1d": EventSpec("market_signal", 2.0, 1.00, 4, "recompute_daily"),
    "earnings_gap_down_unrecovered": EventSpec("market_signal", 2.0, 1.00, 10, "recompute_daily"),
    "volume_anomaly": EventSpec("market_signal", 1.0, 1.00, 5, "recompute_daily"),
    "rel_weakness_5d_20d": EventSpec("market_signal", 1.5, 1.00, None, "recompute_daily"),
    "new_52w_low": EventSpec("market_signal", 1.5, 1.00, None, "recompute_daily"),
    "below_ma50_and_ma200": EventSpec("market_signal", 1.0, 1.00, None, "recompute_daily"),
    "volatility_expansion": EventSpec("market_signal", 1.0, 1.00, 10, "recompute_daily"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def finite_number(value: Any, *, default: float | None = None) -> float | None:
    if value is None or str(value).strip() == "":
        return default
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite number: {value!r}")
    return parsed


def ensure_state_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(STATE_SCHEMA_SQL)
    for table in ("market_signals_daily", "les_snapshots"):
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "market_data_status" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN market_data_status "
                "TEXT NOT NULL DEFAULT 'current' "
                "CHECK (market_data_status IN ('current','missing_latest'))"
            )
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO monitor_state_publication_source_aliases(
                publication_row_sha256,source_manifest_sha256,recorded_at_utc
            )
            SELECT row_sha256,source_manifest_sha256,published_at_utc
            FROM monitor_state_outcome_ledger
            """
        )
        for event_type, spec in sorted(EVENT_SPECS.items()):
            conn.execute(
                """
                INSERT INTO event_taxonomy(
                    event_type,category,default_severity,default_credibility,
                    default_half_life_td,decay_mode,thesis_break_eligible,taxonomy_version
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(event_type) DO UPDATE SET
                    category=excluded.category,
                    default_severity=excluded.default_severity,
                    default_credibility=excluded.default_credibility,
                    default_half_life_td=excluded.default_half_life_td,
                    decay_mode=excluded.decay_mode,
                    thesis_break_eligible=excluded.thesis_break_eligible,
                    taxonomy_version=excluded.taxonomy_version
                """,
                (
                    event_type,
                    spec.category,
                    spec.severity,
                    spec.credibility,
                    spec.half_life_td,
                    spec.decay_mode,
                    int(spec.thesis_break_eligible),
                    TAXONOMY_VERSION,
                ),
            )


def normalize_raw_item(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source", "")).strip().casefold()
    source_uid = str(row.get("source_uid", "")).strip()
    ticker = str(row.get("ticker_hint", "")).strip().upper()
    published = str(row.get("published_at_utc", "")).strip()
    fetched = str(row.get("fetched_at_utc", "")).strip()
    payload = row.get("payload", row.get("payload_json", {}))
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not source or not source_uid or not ticker or not published or not fetched:
        raise ValueError("Raw item lacks source, identity, ticker, or PIT timestamps")
    datetime.fromisoformat(published)
    datetime.fromisoformat(fetched)
    if published > fetched:
        raise ValueError("Raw item was published after it was fetched")
    normalized_payload = canonical_json(payload)
    content_sha = digest(
        {
            "source": source,
            "source_uid": source_uid,
            "ticker": ticker,
            "published_at_utc": published,
            "payload": payload,
        }
    )
    return {
        "item_id": digest({"source": source, "source_uid": source_uid}),
        "source": source,
        "source_uid": source_uid,
        "ticker_hint": ticker,
        "published_at_utc": published,
        "fetched_at_utc": fetched,
        "title": str(row.get("title", "")).strip(),
        "summary": str(row.get("summary", "")).strip(),
        "url": str(row.get("url", "")).strip(),
        "payload_json": normalized_payload,
        "content_sha256": content_sha,
        "status": "new",
    }


def append_raw_items(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    columns = (
        "item_id",
        "source",
        "source_uid",
        "ticker_hint",
        "published_at_utc",
        "fetched_at_utc",
        "title",
        "summary",
        "url",
        "payload_json",
        "content_sha256",
        "status",
    )
    with conn:
        for raw in rows:
            row = normalize_raw_item(raw)
            existing = conn.execute(
                "SELECT content_sha256 FROM raw_items WHERE source=? AND source_uid=?",
                (row["source"], row["source_uid"]),
            ).fetchone()
            if existing is not None:
                if str(existing["content_sha256"]) != row["content_sha256"]:
                    raise RuntimeError(
                        f"Source restatement detected for {row['source']}:{row['source_uid']}"
                    )
                duplicates += 1
                continue
            conn.execute(
                f"INSERT INTO raw_items({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
            inserted += 1
    return inserted, duplicates


def novelty_for_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    event_type: str,
    event_date: str,
    repeat_window_trading_days: int = 20,
    repeat_value: float = 0.30,
) -> float:
    if repeat_window_trading_days < 1 or not 0.0 <= repeat_value <= 1.0:
        raise ValueError("Invalid novelty repeat policy")
    candidates = conn.execute(
        """
        SELECT event_date FROM events
        WHERE ticker=? AND event_type=? AND event_date<?
        ORDER BY event_date DESC
        """,
        (ticker, event_type, event_date),
    ).fetchall()
    repeated = any(
        trading_days_between(str(row["event_date"]), event_date)
        <= repeat_window_trading_days
        for row in candidates
    )
    return repeat_value if repeated else 1.0


def classify_structured_item(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    minimum_estimate_fiscal_period_end: date | None = None,
    novelty_repeat_window_trading_days: int = 20,
    novelty_repeat_value: float = 0.30,
) -> dict[str, Any] | None:
    payload = json.loads(str(row["payload_json"]))
    kind = str(payload.get("kind", "")).strip().casefold()
    direction = finite_number(payload.get("direction"), default=0.0) or 0.0
    event_type = ""
    if kind == "form4_cluster":
        event_type = "insider_buy_cluster" if direction > 0 else "insider_sale_cluster"
    elif kind == "guidance_change":
        event_type = "guidance_raise" if direction > 0 else "guidance_cut" if direction < 0 else "guidance_affirmed"
    elif kind == "estimate_revision":
        event_type = "estimate_revision_up" if direction > 0 else "estimate_revision_down"
    elif kind == "earnings_surprise":
        event_type = "earnings_beat" if direction > 0 else "earnings_miss"
    elif kind in EVENT_SPECS:
        event_type = kind
    if not event_type or event_type not in EVENT_SPECS or direction == 0:
        return None
    if kind == "estimate_revision" and minimum_estimate_fiscal_period_end is not None:
        try:
            fiscal_period_end = date.fromisoformat(
                str(payload.get("fiscal_period_end", ""))
            )
        except ValueError:
            return None
        if fiscal_period_end < minimum_estimate_fiscal_period_end:
            return None
    spec = EVENT_SPECS[event_type]
    ticker = str(row["ticker_hint"])
    event_date = str(payload.get("event_date", str(row["published_at_utc"])[:10]))
    date.fromisoformat(event_date)
    severity = finite_number(payload.get("severity"), default=spec.severity) or spec.severity
    credibility = finite_number(payload.get("credibility"), default=spec.credibility) or spec.credibility
    relevance = finite_number(payload.get("relevance"), default=1.0) or 1.0
    direction = max(-1.0, min(1.0, direction))
    credibility = max(0.0, min(1.0, credibility))
    relevance = max(0.0, min(1.0, relevance))
    novelty = novelty_for_event(
        conn,
        ticker=ticker,
        event_type=event_type,
        event_date=event_date,
        repeat_window_trading_days=novelty_repeat_window_trading_days,
        repeat_value=novelty_repeat_value,
    )
    always_break = event_type in {
        "accounting_restatement_or_material_weakness",
        "balance_sheet_distress",
    }
    source_item_ids = canonical_json([str(row["item_id"])])
    event_key = digest(
        {
            "ticker": ticker,
            "event_type": event_type,
            "event_date": event_date,
            "source_item_ids": source_item_ids,
        }
    )
    return {
        "event_id": event_key,
        "event_key": event_key,
        "ticker": ticker,
        "event_type": event_type,
        "category": spec.category,
        "event_date": event_date,
        "detected_at_utc": str(row["fetched_at_utc"]),
        "direction": direction,
        "severity": severity,
        "credibility": credibility,
        "novelty": novelty,
        "relevance": relevance,
        "impact_0": direction * severity * credibility * novelty * relevance,
        "half_life_td": spec.half_life_td,
        "decay_mode": spec.decay_mode,
        "driver_tag": str(
            payload.get("driver_tag")
            or (
                f"{payload.get('provider', '')}:{payload.get('metric', '')}:"
                f"{payload.get('fiscal_period_end', '')}"
                if kind in {"estimate_revision", "earnings_surprise"}
                else payload.get("metric", "")
            )
        ).strip(),
        "origin_ticker": str(payload.get("origin_ticker", ticker)).strip().upper(),
        "source_item_ids": source_item_ids,
        "classifier": "rule",
        "classifier_version": CLASSIFIER_VERSION,
        "rationale_text": str(payload.get("rationale", f"structured {kind} rule")).strip(),
        "material_flag": int(severity >= 3.5),
        "thesis_break_flag": int(always_break),
        "review_status": "confirmed" if always_break else "auto",
    }


def append_classified_events(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    raw_item_ids: set[str] | None = None,
    minimum_estimate_fiscal_period_end: date | None = None,
    novelty_repeat_window_trading_days: int = 20,
    novelty_repeat_value: float = 0.30,
) -> tuple[list[dict[str, Any]], int]:
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM raw_items WHERE status='new' AND substr(published_at_utc,1,10)<=? ORDER BY published_at_utc,item_id",
            (as_of,),
        ).fetchall()
    ]
    if raw_item_ids is not None:
        rows = [row for row in rows if str(row["item_id"]) in raw_item_ids]
    inserted: list[dict[str, Any]] = []
    irrelevant = 0
    columns = (
        "event_id", "event_key", "ticker", "event_type", "category", "event_date",
        "detected_at_utc", "direction", "severity", "credibility", "novelty", "relevance",
        "impact_0", "half_life_td", "decay_mode", "driver_tag", "origin_ticker",
        "source_item_ids", "classifier", "classifier_version", "rationale_text",
        "material_flag", "thesis_break_flag", "review_status",
    )
    with conn:
        for row in rows:
            event = classify_structured_item(
                conn,
                row,
                minimum_estimate_fiscal_period_end=(
                    minimum_estimate_fiscal_period_end
                ),
                novelty_repeat_window_trading_days=(
                    novelty_repeat_window_trading_days
                ),
                novelty_repeat_value=novelty_repeat_value,
            )
            if event is None:
                conn.execute("UPDATE raw_items SET status='irrelevant' WHERE item_id=?", (row["item_id"],))
                irrelevant += 1
                continue
            existing = conn.execute(
                "SELECT event_id FROM events WHERE event_key=?", (event["event_key"],)
            ).fetchone()
            if existing is None:
                conn.execute(
                    f"INSERT INTO events({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(event[column] for column in columns),
                )
                inserted.append(event)
            conn.execute("UPDATE raw_items SET status='classified' WHERE item_id=?", (row["item_id"],))
    return inserted, irrelevant


def trading_days_between(start: str, end: str) -> int:
    current = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    if finish <= current:
        return 0
    count = 0
    current += timedelta(days=1)
    while current <= finish:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def decayed_event_points(row: dict[str, Any], *, as_of: str, points_per_unit: float) -> float:
    impact = float(row["impact_0"])
    mode = str(row["decay_mode"])
    if mode == "recompute_daily":
        return 0.0
    if mode == "until_replaced":
        decay = 1.0
    else:
        half_life = int(row["half_life_td"] or 0)
        if half_life <= 0:
            return 0.0
        age = trading_days_between(str(row["event_date"]), as_of)
        decay = 0.5 ** (age / half_life)
    return impact * points_per_unit * decay


def internal_state_for(les_total: float, *, confirmed_thesis_break: bool) -> str:
    if les_total < -45.0 and confirmed_thesis_break:
        return "broken"
    if les_total < -25.0:
        return "deteriorating"
    if les_total < -10.0:
        return "watch"
    if les_total < 20.0:
        return "stable"
    return "green"


def action_state_for(
    internal_state: str,
    *,
    is_holding: bool,
    is_target: bool,
    investable: bool,
) -> str:
    if internal_state == "broken":
        return "exit_review"
    if internal_state == "deteriorating":
        return "deteriorating"
    if internal_state == "watch":
        return "suspend_adds" if is_holding or is_target or investable else "watch"
    if internal_state == "green":
        # State evidence alone never authorizes a buy/add. The levels engine owns
        # valuation, liquidity, event-window, and price-zone activation gates.
        return "hold" if is_holding or is_target else "watch"
    return "hold" if is_holding or is_target else "watch"


def outcome_row_hash(previous: str, payload: dict[str, Any]) -> str:
    return digest({"previous_row_sha256": previous, **payload})


def append_state_outcome_rows(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    last = conn.execute(
        "SELECT row_sequence,row_sha256 FROM monitor_state_outcome_ledger ORDER BY row_sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["row_sequence"]) if last is not None else 0
    previous = str(last["row_sha256"]) if last is not None else "0" * 64
    inserted = 0
    duplicates = 0
    with conn:
        for raw in sorted(rows, key=lambda value: (str(value["published_as_of"]), str(value["ticker"]))):
            existing = conn.execute(
                "SELECT * FROM monitor_state_outcome_ledger WHERE ticker=? AND published_as_of=?",
                (raw["ticker"], raw["published_as_of"]),
            ).fetchone()
            if existing is not None:
                expected = {
                    "ticker": str(raw["ticker"]),
                    "published_as_of": str(raw["published_as_of"]),
                    "action_state": str(raw["action_state"]),
                    "internal_state": str(raw["internal_state"]),
                    "les_total": float(raw["les_total"]),
                    "market_price_at_publish": finite_number(raw.get("market_price_at_publish")),
                }
                actual = {key: existing[key] for key in expected}
                if actual != expected:
                    raise RuntimeError(
                        f"First-write-wins state publication drift for {raw['ticker']} {raw['published_as_of']}"
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO monitor_state_publication_source_aliases(
                        publication_row_sha256,source_manifest_sha256,recorded_at_utc
                    ) VALUES(?,?,?)
                    """,
                    (
                        str(existing["row_sha256"]),
                        str(raw["source_manifest_sha256"]),
                        utc_now(),
                    ),
                )
                duplicates += 1
                continue
            sequence += 1
            payload = {
                "row_sequence": sequence,
                "ticker": str(raw["ticker"]),
                "published_as_of": str(raw["published_as_of"]),
                "published_at_utc": str(raw["published_at_utc"]),
                "action_state": str(raw["action_state"]),
                "internal_state": str(raw["internal_state"]),
                "les_total": float(raw["les_total"]),
                "market_price_at_publish": finite_number(raw.get("market_price_at_publish")),
                "source_manifest_sha256": str(raw["source_manifest_sha256"]),
                "resolution_json": "",
                "resolution_available_at_utc": "",
            }
            row_hash = outcome_row_hash(previous, payload)
            conn.execute(
                """
                INSERT INTO monitor_state_outcome_ledger(
                    row_sequence,previous_row_sha256,row_sha256,ticker,published_as_of,
                    published_at_utc,action_state,internal_state,les_total,
                    market_price_at_publish,source_manifest_sha256,resolution_json,
                    resolution_available_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sequence, previous, row_hash, payload["ticker"], payload["published_as_of"],
                    payload["published_at_utc"], payload["action_state"], payload["internal_state"],
                    payload["les_total"], payload["market_price_at_publish"],
                    payload["source_manifest_sha256"], "", "",
                ),
            )
            conn.execute(
                """
                INSERT INTO monitor_state_publication_source_aliases(
                    publication_row_sha256,source_manifest_sha256,recorded_at_utc
                ) VALUES(?,?,?)
                """,
                (row_hash, payload["source_manifest_sha256"], utc_now()),
            )
            previous = row_hash
            inserted += 1
    return inserted, duplicates


def verify_state_outcome_chain(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    previous = "0" * 64
    expected_sequence = 1
    rows = conn.execute(
        "SELECT * FROM monitor_state_outcome_ledger ORDER BY row_sequence"
    ).fetchall()
    for row in rows:
        if int(row["row_sequence"]) != expected_sequence:
            errors.append(f"sequence_gap:{expected_sequence}")
        if str(row["previous_row_sha256"]) != previous:
            errors.append(f"previous_hash_mismatch:{expected_sequence}")
        payload = {
            "row_sequence": int(row["row_sequence"]),
            "ticker": str(row["ticker"]),
            "published_as_of": str(row["published_as_of"]),
            "published_at_utc": str(row["published_at_utc"]),
            "action_state": str(row["action_state"]),
            "internal_state": str(row["internal_state"]),
            "les_total": float(row["les_total"]),
            "market_price_at_publish": row["market_price_at_publish"],
            "source_manifest_sha256": str(row["source_manifest_sha256"]),
            "resolution_json": str(row["resolution_json"]),
            "resolution_available_at_utc": str(row["resolution_available_at_utc"]),
        }
        actual = outcome_row_hash(previous, payload)
        if actual != str(row["row_sha256"]):
            errors.append(f"row_hash_mismatch:{expected_sequence}")
        previous = str(row["row_sha256"])
        expected_sequence += 1
    missing_aliases = conn.execute(
        """
        SELECT COUNT(*)
        FROM monitor_state_outcome_ledger publication
        LEFT JOIN monitor_state_publication_source_aliases alias
          ON alias.publication_row_sha256=publication.row_sha256
         AND alias.source_manifest_sha256=publication.source_manifest_sha256
        WHERE alias.publication_row_sha256 IS NULL
        """
    ).fetchone()[0]
    if int(missing_aliases):
        errors.append(f"publication_source_alias_missing:{missing_aliases}")
    return errors


def append_state_resolution_rows(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    last = conn.execute(
        "SELECT row_sequence,row_sha256 FROM monitor_state_resolution_ledger ORDER BY row_sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["row_sequence"]) if last is not None else 0
    previous = str(last["row_sha256"]) if last is not None else "0" * 64
    inserted = 0
    duplicates = 0
    columns = (
        "row_sequence", "previous_row_sha256", "row_sha256", "publication_row_sha256",
        "ticker", "published_as_of", "resolved_through", "forward_returns_json",
        "sector_excess_returns_json", "maximum_favorable_excursion",
        "maximum_adverse_excursion", "state_changes_json", "event_occurrences_json",
        "resolution_available_at_utc",
    )
    with conn:
        for raw in sorted(rows, key=lambda value: (str(value["published_as_of"]), str(value["ticker"]))):
            publication_sha = str(raw["publication_row_sha256"])
            existing = conn.execute(
                "SELECT * FROM monitor_state_resolution_ledger WHERE publication_row_sha256=?",
                (publication_sha,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "publication_row_sha256": publication_sha,
                    "ticker": str(raw["ticker"]),
                    "published_as_of": str(raw["published_as_of"]),
                    "resolved_through": str(raw["resolved_through"]),
                    "forward_returns_json": str(raw["forward_returns_json"]),
                    "sector_excess_returns_json": str(raw["sector_excess_returns_json"]),
                    "maximum_favorable_excursion": float(raw["maximum_favorable_excursion"]),
                    "maximum_adverse_excursion": float(raw["maximum_adverse_excursion"]),
                    "state_changes_json": str(raw["state_changes_json"]),
                    "event_occurrences_json": str(raw["event_occurrences_json"]),
                }
                actual = {key: existing[key] for key in expected}
                if actual != expected:
                    raise RuntimeError(
                        f"First-write-wins state resolution drift for {raw['ticker']} "
                        f"{raw['published_as_of']}"
                    )
                duplicates += 1
                continue
            sequence += 1
            payload = {
                "row_sequence": sequence,
                "publication_row_sha256": publication_sha,
                "ticker": str(raw["ticker"]),
                "published_as_of": str(raw["published_as_of"]),
                "resolved_through": str(raw["resolved_through"]),
                "forward_returns_json": str(raw["forward_returns_json"]),
                "sector_excess_returns_json": str(raw["sector_excess_returns_json"]),
                "maximum_favorable_excursion": float(raw["maximum_favorable_excursion"]),
                "maximum_adverse_excursion": float(raw["maximum_adverse_excursion"]),
                "state_changes_json": str(raw["state_changes_json"]),
                "event_occurrences_json": str(raw["event_occurrences_json"]),
                "resolution_available_at_utc": str(raw["resolution_available_at_utc"]),
            }
            row_hash = outcome_row_hash(previous, payload)
            values = {
                **payload,
                "previous_row_sha256": previous,
                "row_sha256": row_hash,
            }
            conn.execute(
                f"INSERT INTO monitor_state_resolution_ledger({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            previous = row_hash
            inserted += 1
    return inserted, duplicates


def verify_state_resolution_chain(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    previous = "0" * 64
    expected_sequence = 1
    for row in conn.execute(
        "SELECT * FROM monitor_state_resolution_ledger ORDER BY row_sequence"
    ).fetchall():
        payload = {
            "row_sequence": int(row["row_sequence"]),
            "publication_row_sha256": str(row["publication_row_sha256"]),
            "ticker": str(row["ticker"]),
            "published_as_of": str(row["published_as_of"]),
            "resolved_through": str(row["resolved_through"]),
            "forward_returns_json": str(row["forward_returns_json"]),
            "sector_excess_returns_json": str(row["sector_excess_returns_json"]),
            "maximum_favorable_excursion": float(row["maximum_favorable_excursion"]),
            "maximum_adverse_excursion": float(row["maximum_adverse_excursion"]),
            "state_changes_json": str(row["state_changes_json"]),
            "event_occurrences_json": str(row["event_occurrences_json"]),
            "resolution_available_at_utc": str(row["resolution_available_at_utc"]),
        }
        if int(row["row_sequence"]) != expected_sequence:
            errors.append(f"resolution_sequence_gap:{expected_sequence}")
        if str(row["previous_row_sha256"]) != previous:
            errors.append(f"resolution_previous_hash_mismatch:{expected_sequence}")
        if outcome_row_hash(previous, payload) != str(row["row_sha256"]):
            errors.append(f"resolution_row_hash_mismatch:{expected_sequence}")
        previous = str(row["row_sha256"])
        expected_sequence += 1
    return errors
