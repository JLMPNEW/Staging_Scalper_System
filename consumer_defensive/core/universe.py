from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from consumer_defensive.core.db import execute_schema_script, utc_now
from consumer_defensive.core.tickers import validate_investable_ticker


MODEL_FAMILY = "consumer_defensive"
INTERNAL_SECTOR = "Consumer Defensive"
PORTFOLIO_SECTOR = "Consumer Staples"
CURRENT_SOURCE_ID = "consumer_defensive_current_universe"
ALIAS_SOURCE_ID = "consumer_defensive_ticker_aliases_reviewed"
SECURITY_EVENT_SOURCE_ID = "consumer_defensive_security_events_reviewed"
PIT_SOURCE_ID = "norgate_us_equities_pit_membership"

STAGE2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_recognized_vehicle (
    vehicle_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    vehicle_type TEXT NOT NULL,
    provider_index_name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS dim_security_alias (
    alias_ticker TEXT NOT NULL,
    canonical_security_id INTEGER NOT NULL,
    canonical_ticker TEXT NOT NULL,
    relationship TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    provider_history_owner TEXT NOT NULL,
    load_as_separate_security INTEGER NOT NULL DEFAULT 0
        CHECK(load_as_separate_security IN (0, 1)),
    source_id TEXT,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(alias_ticker, canonical_ticker, valid_from),
    FOREIGN KEY(canonical_security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_recognized_vehicle_membership_daily (
    security_id INTEGER NOT NULL,
    provider_asset_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    membership_date TEXT NOT NULL,
    member_flag INTEGER NOT NULL CHECK(member_flag IN (0, 1)),
    source_id TEXT NOT NULL,
    provider_database_updated_at TEXT,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY(security_id, vehicle_id, membership_date),
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    FOREIGN KEY(vehicle_id) REFERENCES dim_recognized_vehicle(vehicle_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_major_exchange_listing_daily (
    security_id INTEGER NOT NULL,
    provider_asset_id TEXT NOT NULL,
    listing_date TEXT NOT NULL,
    major_exchange_listed_flag INTEGER NOT NULL CHECK(major_exchange_listed_flag IN (0, 1)),
    source_id TEXT NOT NULL,
    provider_database_updated_at TEXT,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY(security_id, listing_date),
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_cd_alias_canonical
    ON dim_security_alias(canonical_ticker, alias_ticker);
CREATE INDEX IF NOT EXISTS idx_cd_vehicle_membership_date
    ON fact_recognized_vehicle_membership_daily(membership_date, vehicle_id, member_flag);
CREATE INDEX IF NOT EXISTS idx_cd_major_exchange_date
    ON fact_major_exchange_listing_daily(listing_date, major_exchange_listed_flag);
"""


@dataclass(frozen=True)
class UniversePolicy:
    path: Path
    payload: dict[str, Any]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, key: str) -> Path:
        raw = self.payload.get(key)
        if not raw:
            raise ValueError(f"Policy path {key!r} is missing.")
        value = Path(str(raw)).expanduser()
        return value.resolve() if value.is_absolute() else (self.base_dir / value).resolve()


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for the Consumer Defensive universe policy.") from exc
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload


def load_policy(path: Path) -> UniversePolicy:
    resolved = path.expanduser().resolve()
    payload = read_yaml(resolved)
    expected = {
        "model_family": MODEL_FAMILY,
        "internal_sector": INTERNAL_SECTOR,
        "portfolio_sector": PORTFOLIO_SECTOR,
        "recognized_membership_required": True,
        "recognized_membership_source_id": PIT_SOURCE_ID,
        "history_start": "2017-11-28",
        "requested_snapshot_start": "2019-01-02",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"Universe policy {key} must be {value!r}; got {payload.get(key)!r}.")
    vehicles = payload.get("approved_membership_vehicles")
    if not isinstance(vehicles, list):
        raise ValueError("approved_membership_vehicles must be a list.")
    ids = [str(row.get("vehicle_id") or "") for row in vehicles if isinstance(row, dict)]
    expected_ids = {
        "russell_3000",
        "sp_composite_1500",
        "nyse_composite",
        "nasdaq_composite",
    }
    if set(ids) != expected_ids or len(ids) != len(expected_ids):
        raise ValueError("The policy must define exactly the four approved membership indices.")
    if any(
        not str(row.get("norgate_index_name") or "") or not str(row.get("norgate_watchlist_name") or "")
        for row in vehicles
    ):
        raise ValueError("Every approved vehicle requires provider index and Current & Past watchlist names.")
    if not payload.get("terminal_event_policy"):
        raise ValueError("The universe policy must declare the reviewed terminal-event policy path.")
    if payload.get("current_holdings_validation_only") != ["XLP", "IYK", "FSTA"]:
        raise ValueError("XLP, IYK, and FSTA must remain validation-only sources.")
    return UniversePolicy(path=resolved, payload=payload)


def read_csv(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [
                    {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}
                    for row in reader
                ]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode {path}: {last_error}")


def truthy(raw: Any) -> int:
    return int(str(raw or "").strip().casefold() in {"1", "true", "yes", "y"})


def active_universe_tickers(conn: sqlite3.Connection) -> list[str]:
    """Canonical current-universe selector for this independent sector."""
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT s.ticker
            FROM dim_security s
            JOIN dim_company c ON c.company_id=s.company_id
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=s.security_id
            WHERE t.model_family=? AND s.listing_status='active' AND c.is_active=1
            ORDER BY s.ticker
            """,
            (MODEL_FAMILY,),
        ).fetchall()
    ]


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper()


def normalize_security_type(raw: Any) -> str:
    value = str(raw or "").strip()
    key = value.casefold().replace(" ", "")
    if key in {"adr", "ads", "adr/ads", "adrads"}:
        return "ADR/ADS"
    if key in {"ordinaryshares", "ordinaryshare"}:
        return "Ordinary Shares"
    if key in {"commonstock", "commonshares", "commonshare"}:
        return "Common Stock"
    return value


def ensure_stage2_schema(conn: sqlite3.Connection) -> None:
    execute_schema_script(conn, STAGE2_SCHEMA_SQL)


def upsert_stage2_sources(conn: sqlite3.Connection, rows: Iterable[Any]) -> int:
    now = utc_now()
    count = 0
    with conn:
        for row in rows:
            columns = tuple(row.__dataclass_fields__)
            values = tuple(getattr(row, column) for column in columns)
            assignments = ", ".join(
                f"{column}=excluded.{column}" for column in columns if column != "source_id"
            )
            conn.execute(
                f"INSERT INTO source_registry({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in columns)}, ?, ?) "
                f"ON CONFLICT(source_id) DO UPDATE SET {assignments}, updated_at=excluded.updated_at",
                (*values, now, now),
            )
            count += 1
    return count


def validate_current_rows(rows: list[dict[str, str]], policy: UniversePolicy) -> None:
    expected_count = int(policy.payload["expected_current_rows"])
    if len(rows) != expected_count:
        raise ValueError(f"Current universe must contain {expected_count} rows; found {len(rows)}.")
    required = {
        "ticker",
        "investability_status",
        "company_name",
        "cik",
        "exchange",
        "sector",
        "industry",
        "country",
        "currency",
        "security_type",
        "listing_status",
        "is_primary_listing",
    }
    missing_columns = sorted(required.difference(rows[0] if rows else {}))
    if missing_columns:
        raise ValueError(f"Current universe is missing columns: {missing_columns}")
    tickers = [
        validate_investable_ticker(row["ticker"], context='current-universe ticker')
        for row in rows
    ]
    duplicates = sorted(ticker for ticker, n in Counter(tickers).items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate live tickers: {duplicates}")
    if "CENT" in tickers or "CENTA" not in tickers:
        raise ValueError("The reviewed liquid class decision requires CENTA and excludes CENT.")
    if "DMC" not in tickers or "BOOM" in tickers:
        raise ValueError("DMC must identify Del Monte Corporation; DMC Global/BOOM is out of scope.")
    allowed_types = set(policy.payload["allowed_security_types"])
    allowed_statuses = {str(x).casefold() for x in policy.payload["active_listing_statuses"]}
    cohort_policy = policy.payload["cohorts"]
    observed = Counter()
    errors: list[str] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        validate_investable_ticker(ticker, context='current-universe ticker')
        security_type = normalize_security_type(row["security_type"])
        industry = row["industry"]
        if any(not row[column] for column in required):
            errors.append(f"{ticker}:missing_required_value")
        if row["sector"] != INTERNAL_SECTOR:
            errors.append(f"{ticker}:sector={row['sector']!r}")
        if row["investability_status"].casefold() != "investable":
            errors.append(f"{ticker}:investability_status={row['investability_status']!r}")
        if row["listing_status"].casefold() not in allowed_statuses:
            errors.append(f"{ticker}:listing_status={row['listing_status']!r}")
        if security_type not in allowed_types:
            errors.append(f"{ticker}:security_type={security_type!r}")
        if truthy(row["is_primary_listing"]) != 1:
            errors.append(f"{ticker}:not_primary_listing")
        if industry not in cohort_policy:
            errors.append(f"{ticker}:unknown_industry={industry!r}")
        else:
            observed[industry] += 1
    for industry, config in cohort_policy.items():
        expected = int(config["expected_current_rows"])
        if observed[industry] != expected:
            errors.append(f"{industry}:expected={expected}:actual={observed[industry]}")
    if errors:
        raise ValueError("Current universe validation failed: " + "; ".join(errors[:30]))


def _upsert_company(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    now = utc_now()
    ticker = normalize_ticker(row["ticker"])
    conn.execute(
        """
        INSERT INTO dim_company(
            primary_ticker, cik, company_name, issuer_domicile, reporting_currency,
            universe_status, is_active, data_quality_status, first_seen_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, 'keep', 1, 'complete', ?, ?)
        ON CONFLICT(primary_ticker) DO UPDATE SET
            cik=excluded.cik,
            company_name=excluded.company_name,
            reporting_currency=excluded.reporting_currency,
            universe_status='keep',
            is_active=1,
            data_quality_status='complete',
            updated_at=excluded.updated_at
        """,
        (ticker, row["cik"], row["company_name"], row["currency"], now, now),
    )
    result = conn.execute(
        "SELECT company_id FROM dim_company WHERE primary_ticker = ?", (ticker,)
    ).fetchone()
    if result is None:
        raise RuntimeError(f"Failed to load dim_company for {ticker}.")
    return int(result[0])


def _upsert_security(conn: sqlite3.Connection, row: dict[str, str], company_id: int) -> int:
    now = utc_now()
    ticker = normalize_ticker(row["ticker"])
    existing = conn.execute(
        "SELECT security_id FROM dim_security WHERE ticker = ? AND listing_status = 'active'",
        (ticker,),
    ).fetchall()
    if len(existing) > 1:
        raise ValueError(f"Multiple live security rows found for {ticker}.")
    values = (
        company_id,
        ticker,
        ticker,
        row["exchange"],
        row["country"],
        normalize_security_type(row["security_type"]),
        int(normalize_security_type(row["security_type"]) == "ADR/ADS"),
        "active",
        1,
        row["currency"],
        now,
    )
    if existing:
        security_id = int(existing[0][0])
        conn.execute(
            """
            UPDATE dim_security SET
                company_id=?, ticker=?, exchange=?, listing_country=?,
                security_type=?, adr_ads_flag=?, listing_status=?, is_primary_listing=?,
                currency=?, updated_at=?
            WHERE security_id=?
            """,
            (
                company_id,
                ticker,
                row['exchange'],
                row['country'],
                normalize_security_type(row['security_type']),
                int(normalize_security_type(row['security_type']) == 'ADR/ADS'),
                'active',
                1,
                row['currency'],
                now,
                security_id,
            ),
        )
        return security_id
    cursor = conn.execute(
        """
        INSERT INTO dim_security(
            company_id, ticker, provider_price_symbol, exchange, listing_country,
            security_type, adr_ads_flag, listing_status, is_primary_listing,
            currency, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values[:-1], now, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to load dim_security for {ticker}.")
    return int(cursor.lastrowid)


def _upsert_taxonomy(
    conn: sqlite3.Connection,
    row: dict[str, str],
    company_id: int,
    security_id: int,
    policy: UniversePolicy,
) -> None:
    now = utc_now()
    ticker = normalize_ticker(row["ticker"])
    cohort = policy.payload["cohorts"][row["industry"]]
    conn.execute(
        """
        INSERT INTO dim_consumer_defensive_taxonomy(
            company_id, security_id, ticker, model_family, sector, portfolio_sector,
            calibration_cohort_id, calibration_cohort, applicability_subtype,
            taxonomy_confidence, taxonomy_source, business_cohort_override_flag,
            analyst_reviewed, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 1.0, ?, 0, 1, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            company_id=excluded.company_id,
            security_id=excluded.security_id,
            sector=excluded.sector,
            portfolio_sector=excluded.portfolio_sector,
            calibration_cohort_id=excluded.calibration_cohort_id,
            calibration_cohort=excluded.calibration_cohort,
            taxonomy_confidence=excluded.taxonomy_confidence,
            taxonomy_source=excluded.taxonomy_source,
            analyst_reviewed=excluded.analyst_reviewed,
            updated_at=excluded.updated_at
        """,
        (
            company_id,
            security_id,
            ticker,
            MODEL_FAMILY,
            INTERNAL_SECTOR,
            PORTFOLIO_SECTOR,
            cohort["cohort_id"],
            str(cohort.get("display_name") or row["industry"]),
            CURRENT_SOURCE_ID,
            now,
        ),
    )


def upsert_vehicles(conn: sqlite3.Connection, policy: UniversePolicy) -> int:
    now = utc_now()
    rows = policy.payload["approved_membership_vehicles"]
    for row in rows:
        conn.execute(
            """
            INSERT INTO dim_recognized_vehicle(
                vehicle_id, display_name, vehicle_type, provider_index_name,
                source_id, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(vehicle_id) DO UPDATE SET
                display_name=excluded.display_name,
                vehicle_type=excluded.vehicle_type,
                provider_index_name=excluded.provider_index_name,
                source_id=excluded.source_id,
                is_active=1,
                updated_at=excluded.updated_at
            """,
            (
                row["vehicle_id"],
                row["display_name"],
                row["vehicle_type"],
                row["norgate_index_name"],
                PIT_SOURCE_ID,
                now,
                now,
            ),
        )
    return len(rows)


def load_aliases_and_events(conn: sqlite3.Connection, policy: UniversePolicy) -> tuple[int, int]:
    now = utc_now()
    alias_rows = read_csv(policy.resolve("lineage_aliases_csv"))
    event_rows = read_csv(policy.resolve("security_events_csv"))
    live_tickers = {
        str(row[0])
        for row in conn.execute(
            "SELECT ticker FROM dim_security WHERE listing_status = 'active'"
        ).fetchall()
    }
    alias_count = 0
    for row in alias_rows:
        alias = normalize_ticker(row["alias_ticker"])
        canonical = normalize_ticker(row["canonical_ticker"])
        validate_investable_ticker(alias, context='reviewed alias ticker')
        validate_investable_ticker(canonical, context='alias canonical ticker')
        if alias in live_tickers:
            raise ValueError(f"Alias ticker {alias} collides with the live universe.")
        security = conn.execute(
            "SELECT security_id FROM dim_security WHERE ticker = ? AND listing_status = 'active'",
            (canonical,),
        ).fetchone()
        if security is None:
            raise ValueError(f"Alias {alias} references missing canonical ticker {canonical}.")
        if truthy(row["load_as_separate_security"]):
            raise ValueError(f"Lineage alias {alias} must not load as a separate security.")
        conn.execute(
            """
            INSERT INTO dim_security_alias(
                alias_ticker, canonical_security_id, canonical_ticker, relationship,
                valid_from, valid_to, provider_history_owner, load_as_separate_security,
                source_id, source_detail, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(alias_ticker, canonical_ticker, valid_from) DO UPDATE SET
                canonical_security_id=excluded.canonical_security_id,
                relationship=excluded.relationship,
                valid_to=excluded.valid_to,
                provider_history_owner=excluded.provider_history_owner,
                load_as_separate_security=0,
                source_id=excluded.source_id,
                source_detail=excluded.source_detail,
                updated_at=excluded.updated_at
            """,
            (
                alias,
                int(security[0]),
                canonical,
                row["relationship"],
                row["valid_from"],
                row["valid_to"],
                row["provider_history_owner"],
                ALIAS_SOURCE_ID,
                json.dumps({"source_url": row["source_url"], "notes": row["notes"]}),
                now,
                now,
            ),
        )
        alias_count += 1
    event_count = 0
    for row in event_rows:
        canonical = normalize_ticker(row["canonical_ticker"])
        historical = normalize_ticker(row["historical_ticker"])
        validate_investable_ticker(canonical, context='event canonical ticker')
        validate_investable_ticker(historical, context='event historical ticker')
        security = conn.execute(
            "SELECT security_id FROM dim_security WHERE ticker = ? AND listing_status = 'active'",
            (canonical,),
        ).fetchone()
        if security is None:
            raise ValueError(f"Event {historical} references missing canonical ticker {canonical}.")
        conn.execute(
            """
            INSERT INTO fact_security_event(
                security_id, ticker, event_type, event_date, last_trade_date,
                successor_ticker, stock_consideration_json, survivorship_complete,
                source_id, source_detail, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, event_type, event_date) DO UPDATE SET
                security_id=excluded.security_id,
                last_trade_date=excluded.last_trade_date,
                successor_ticker=excluded.successor_ticker,
                stock_consideration_json=excluded.stock_consideration_json,
                survivorship_complete=excluded.survivorship_complete,
                source_id=excluded.source_id,
                source_detail=excluded.source_detail,
                updated_at=excluded.updated_at
            """,
            (
                int(security[0]),
                historical,
                row["event_type"],
                row["event_date"],
                row["last_trade_date"],
                row["successor_ticker"],
                json.dumps({"terminal_type": row["terminal_type"]}),
                truthy(row["survivorship_complete"]),
                SECURITY_EVENT_SOURCE_ID,
                json.dumps({"source_url": row["source_url"], "notes": row["notes"]}),
                now,
                now,
            ),
        )
        event_count += 1
    return alias_count, event_count


def load_current_universe(
    conn: sqlite3.Connection,
    policy: UniversePolicy,
    current_csv: Path | None = None,
) -> dict[str, int]:
    path = current_csv.expanduser().resolve() if current_csv else policy.resolve("authoritative_current_csv")
    rows = read_csv(path)
    validate_current_rows(rows, policy)
    ensure_stage2_schema(conn)
    incoming_tickers = {normalize_ticker(row["ticker"]) for row in rows}
    with conn:
        existing_current = {
            str(row[0])
            for row in conn.execute(
                """SELECT t.ticker
                   FROM dim_consumer_defensive_taxonomy t
                   JOIN dim_security s ON s.security_id=t.security_id
                   JOIN dim_company c ON c.company_id=t.company_id
                   WHERE t.model_family=? AND s.listing_status='active' AND c.is_active=1""",
                (MODEL_FAMILY,),
            )
        }
        stale_tickers = sorted(existing_current - incoming_tickers)
        if stale_tickers:
            placeholders = ",".join("?" for _ in stale_tickers)
            conn.execute(
                f"""DELETE FROM dim_consumer_defensive_taxonomy
                    WHERE model_family=? AND ticker IN ({placeholders})""",
                [MODEL_FAMILY, *stale_tickers],
            )
        upsert_vehicles(conn, policy)
        for row in rows:
            company_id = _upsert_company(conn, row)
            security_id = _upsert_security(conn, row, company_id)
            _upsert_taxonomy(conn, row, company_id, security_id, policy)
        aliases, events = load_aliases_and_events(conn, policy)
    return {
        "current_rows": len(rows),
        "vehicles": len(policy.payload["approved_membership_vehicles"]),
        "aliases": aliases,
        "events": events,
        "stale_taxonomy_rows_removed": len(stale_tickers),
    }
