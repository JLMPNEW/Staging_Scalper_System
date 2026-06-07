from __future__ import annotations

import argparse
import csv
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\market_positioning.sqlite")
DEFAULT_HISTORY_START_DATE = date(2019, 1, 1)
SEC_PUBLIC_FLOAT_FORMS = ("10-K", "10-K/A")
SEC_PUBLIC_FLOAT_SOURCE = "sec_10k_public_float_proxy"

ENTITY_PUBLIC_FLOAT_RE = re.compile(
    r"EntityPublicFloat.{0,1500}?>(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)<",
    re.IGNORECASE | re.DOTALL,
)
AGGREGATE_MARKET_VALUE_RE = re.compile(
    r"aggregate\s+market\s+value.{0,1400}?non[-\s]?affiliates?.{0,900}?"
    r"(?:\$|US\$)\s*(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<scale>billion|million|thousand|bn|mm|m|k)?",
    re.IGNORECASE | re.DOTALL,
)
PUBLIC_FLOAT_CONTEXT_RE = re.compile(
    r"(?:public\s+float|aggregate\s+market\s+value).{0,1600}?"
    r"(?:\$|US\$)\s*(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<scale>billion|million|thousand|bn|mm|m|k)?",
    re.IGNORECASE | re.DOTALL,
)
AS_OF_DATE_RE = re.compile(
    r"as\s+of(?:\s+the\s+close\s+of\s+business\s+on)?\s+"
    r"(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_ticker(raw: object) -> str:
    text = str(raw or "").strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return ""
    return text.replace(".", "-")


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y%m%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text[: min(10, len(text))], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_float(raw: object, default: float | None = None) -> float | None:
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def normalize_pct(raw: object, default: float | None = None) -> float | None:
    value = to_float(raw, default)
    if value is None:
        return default
    return value / 100.0 if abs(value) > 2.0 else value


def first_present(row: dict[str, Any], *keys: str) -> object:
    for key in keys:
        value = row.get(key)
        if str(value if value is not None else "").strip():
            return value
    return ""


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS short_interest_snapshots (
                ticker TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                settlement_date TEXT,
                publication_date TEXT,
                short_interest_shares REAL,
                float_shares REAL,
                short_interest_pct_float REAL,
                days_to_cover REAL,
                source TEXT NOT NULL DEFAULT 'csv',
                source_file TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, settlement_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_short_interest_ticker_asof
                ON short_interest_snapshots(ticker, asof_date);

            CREATE TABLE IF NOT EXISTS float_shares_snapshots (
                ticker TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                float_shares REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'csv',
                source_file TEXT,
                source_asof_date TEXT,
                source_filing_date TEXT,
                source_accession_nodash TEXT,
                public_float_usd REAL,
                public_float_measurement_date TEXT,
                close_price REAL,
                price_date TEXT,
                proxy_flag REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_float_shares_ticker_asof
                ON float_shares_snapshots(ticker, asof_date);

            CREATE TABLE IF NOT EXISTS institutional_13f_filings (
                filing_key TEXT PRIMARY KEY,
                accession_number TEXT,
                manager_cik TEXT,
                manager_name TEXT,
                period_of_report TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                accepted_at TEXT,
                source TEXT NOT NULL DEFAULT 'csv',
                source_file TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS institutional_13f_holdings (
                filing_key TEXT NOT NULL,
                manager_cik TEXT,
                manager_name TEXT,
                ticker TEXT NOT NULL,
                cusip TEXT,
                period_of_report TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                accepted_at TEXT,
                shares REAL,
                market_value REAL,
                title_of_class TEXT DEFAULT '',
                share_type TEXT DEFAULT '',
                put_call TEXT DEFAULT '',
                source TEXT NOT NULL DEFAULT 'csv',
                source_file TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (filing_key, ticker, cusip),
                FOREIGN KEY (filing_key) REFERENCES institutional_13f_filings(filing_key)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_13f_holdings_ticker_filing
                ON institutional_13f_holdings(ticker, filing_date);

            CREATE TABLE IF NOT EXISTS institutional_13f_ownership_snapshots (
                ticker TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                period_of_report TEXT,
                institutional_shares REAL,
                institutional_value REAL,
                manager_count INTEGER,
                new_buyer_count INTEGER,
                exiting_holder_count INTEGER,
                net_buyer_count INTEGER,
                institutional_ownership_delta_pct REAL,
                source TEXT NOT NULL DEFAULT 'csv',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_13f_ownership_ticker_asof
                ON institutional_13f_ownership_snapshots(ticker, asof_date);

            CREATE TABLE IF NOT EXISTS ibkr_borrow_fee_rate_daily (
                ticker TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                con_id INTEGER,
                borrow_fee_rate REAL,
                source TEXT NOT NULL DEFAULT 'interactive_brokers',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_ibkr_borrow_fee_ticker_asof
                ON ibkr_borrow_fee_rate_daily(ticker, asof_date);

            CREATE TABLE IF NOT EXISTS ibkr_shortable_shares_snapshots (
                ticker TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                asof_datetime TEXT NOT NULL,
                con_id INTEGER,
                shortable_shares REAL,
                market_data_type INTEGER,
                source TEXT NOT NULL DEFAULT 'interactive_brokers',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_ibkr_shortable_ticker_asof
                ON ibkr_shortable_shares_snapshots(ticker, asof_date);

            CREATE TABLE IF NOT EXISTS market_positioning_feed_state (
                feed_name TEXT PRIMARY KEY,
                last_success_at TEXT NOT NULL,
                history_start_date TEXT,
                source TEXT,
                source_file TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            """
        )
        ensure_table_columns(
            conn,
            "float_shares_snapshots",
            {
                "source_asof_date": "TEXT",
                "source_filing_date": "TEXT",
                "source_accession_nodash": "TEXT",
                "public_float_usd": "REAL",
                "public_float_measurement_date": "TEXT",
                "close_price": "REAL",
                "price_date": "TEXT",
                "proxy_flag": "REAL NOT NULL DEFAULT 0.0",
            },
        )
        ensure_table_columns(
            conn,
            "institutional_13f_holdings",
            {
                "title_of_class": "TEXT DEFAULT ''",
                "share_type": "TEXT DEFAULT ''",
                "put_call": "TEXT DEFAULT ''",
            },
        )
        ensure_table_columns(
            conn,
            "institutional_13f_ownership_snapshots",
            {
                "new_buyer_count": "INTEGER",
                "exiting_holder_count": "INTEGER",
                "net_buyer_count": "INTEGER",
            },
        )


def ensure_table_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, column_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def update_feed_state(
    conn: sqlite3.Connection,
    *,
    feed_name: str,
    history_start_date: date,
    source: str,
    source_file: Path | None,
    row_count: int,
    message: str = "",
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO market_positioning_feed_state(
                feed_name, last_success_at, history_start_date, source, source_file, row_count, message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(feed_name) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                history_start_date = excluded.history_start_date,
                source = excluded.source,
                source_file = excluded.source_file,
                row_count = excluded.row_count,
                message = excluded.message
            """,
            (
                feed_name,
                utc_now(),
                history_start_date.isoformat(),
                source,
                str(source_file or ""),
                row_count,
                message,
            ),
        )


def ingest_short_interest_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    history_start_date: date = DEFAULT_HISTORY_START_DATE,
    publication_lag_days: int = 8,
    source: str = "csv",
) -> int:
    rows = read_csv_rows(csv_path)
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    for row in rows:
        ticker = normalize_ticker(first_present(row, "ticker", "symbol"))
        if not ticker:
            continue
        settlement = parse_date(first_present(row, "settlement_date", "settle_date", "asof_date", "date"))
        publication = parse_date(first_present(row, "publication_date", "published_date", "available_date"))
        if settlement is None and publication is None:
            continue
        if publication is None and settlement is not None:
            publication = settlement + timedelta(days=max(0, publication_lag_days))
        asof = parse_date(first_present(row, "asof_date", "available_date")) or publication or settlement
        if asof is None or asof < history_start_date:
            continue
        short_pct = normalize_pct(
            first_present(row, "short_interest_pct_float", "short_percent_float", "short_interest_pct", "short_interest_percent_float")
        )
        short_shares = to_float(first_present(row, "short_interest_shares", "short_shares"))
        float_shares = to_float(first_present(row, "float_shares", "shares_float"))
        if short_pct is None and short_shares is not None and float_shares and float_shares > 0.0:
            short_pct = short_shares / float_shares
        records.append(
            (
                ticker,
                asof.isoformat(),
                settlement.isoformat() if settlement else "",
                publication.isoformat() if publication else "",
                short_shares,
                float_shares,
                short_pct,
                to_float(first_present(row, "days_to_cover", "short_ratio")),
                str(first_present(row, "source") or source),
                str(csv_path),
                now,
                now,
            )
        )
    with conn:
        conn.executemany(
            """
            INSERT INTO short_interest_snapshots(
                ticker, asof_date, settlement_date, publication_date,
                short_interest_shares, float_shares, short_interest_pct_float, days_to_cover,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, settlement_date, source) DO UPDATE SET
                publication_date = excluded.publication_date,
                short_interest_shares = excluded.short_interest_shares,
                float_shares = excluded.float_shares,
                short_interest_pct_float = excluded.short_interest_pct_float,
                days_to_cover = excluded.days_to_cover,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            records,
        )
    update_feed_state(
        conn,
        feed_name="short_interest",
        history_start_date=history_start_date,
        source=source,
        source_file=csv_path,
        row_count=len(records),
    )
    return len(records)


def ingest_float_shares_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    history_start_date: date = DEFAULT_HISTORY_START_DATE,
    source: str = "csv",
) -> int:
    """Load point-in-time float-shares snapshots used to enrich short interest.

    Accepted columns are intentionally broad because float-share vendors use
    different names. Each row must include ticker/symbol, a date, and a positive
    float-shares value.
    """
    rows = read_csv_rows(csv_path)
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    for row in rows:
        ticker = normalize_ticker(first_present(row, "ticker", "symbol"))
        if not ticker:
            continue
        asof = parse_date(first_present(row, "asof_date", "date", "effective_date", "snapshot_date", "publication_date"))
        if asof is None or asof < history_start_date:
            continue
        shares = to_float(
            first_present(
                row,
                "float_shares",
                "shares_float",
                "public_float_shares",
                "free_float_shares",
                "public_float",
                "float",
            )
        )
        if shares is None or shares <= 0.0:
            continue
        source_asof = parse_date(first_present(row, "source_asof_date", "measurement_date", "period_end")) or asof
        source_filing = parse_date(first_present(row, "source_filing_date", "filing_date", "filed_date"))
        proxy_flag = to_float(first_present(row, "proxy_flag", "is_proxy"), None)
        if proxy_flag is None:
            proxy_flag = 1.0 if "proxy" in str(first_present(row, "source") or source).lower() else 0.0
        records.append(
            (
                ticker,
                asof.isoformat(),
                shares,
                str(first_present(row, "source") or source),
                str(csv_path),
                source_asof.isoformat() if source_asof else None,
                source_filing.isoformat() if source_filing else None,
                None,
                None,
                source_asof.isoformat() if source_asof else None,
                None,
                None,
                1.0 if proxy_flag > 0.0 else 0.0,
                now,
                now,
            )
        )
    with conn:
        conn.executemany(
            """
            INSERT INTO float_shares_snapshots(
                ticker, asof_date, float_shares, source, source_file,
                source_asof_date, source_filing_date, source_accession_nodash,
                public_float_usd, public_float_measurement_date, close_price, price_date,
                proxy_flag, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                float_shares = excluded.float_shares,
                source_file = excluded.source_file,
                source_asof_date = excluded.source_asof_date,
                source_filing_date = excluded.source_filing_date,
                source_accession_nodash = excluded.source_accession_nodash,
                public_float_usd = excluded.public_float_usd,
                public_float_measurement_date = excluded.public_float_measurement_date,
                close_price = excluded.close_price,
                price_date = excluded.price_date,
                proxy_flag = excluded.proxy_flag,
                updated_at = excluded.updated_at
            """,
            records,
        )
    update_feed_state(
        conn,
        feed_name="float_shares",
        history_start_date=history_start_date,
        source=source,
        source_file=csv_path,
        row_count=len(records),
    )
    return len(records)


def scaled_money_value(raw_value: object, scale: object = "") -> float | None:
    value = to_float(raw_value)
    if value is None or value <= 0.0:
        return None
    clean_scale = str(scale or "").strip().lower().replace(".", "")
    multiplier = 1.0
    if clean_scale in {"billion", "bn", "b"}:
        multiplier = 1_000_000_000.0
    elif clean_scale in {"million", "mm", "m"}:
        multiplier = 1_000_000.0
    elif clean_scale in {"thousand", "k"}:
        multiplier = 1_000.0
    return value * multiplier


def previous_business_day(day: date) -> date:
    out = day
    while out.weekday() >= 5:
        out = out - timedelta(days=1)
    return out


def infer_public_float_measurement_date(filing_date: date) -> tuple[date, str]:
    """Fallback to the common 10-K public-float measurement date.

    U.S. 10-K cover pages generally disclose public float measured on the last
    business day of the registrant's second fiscal quarter.  Most companies in
    this universe use a calendar fiscal year, so June 30 of the prior year is a
    conservative fallback when the exact date is not parsed from the cover page.
    """
    year = filing_date.year - 1 if filing_date.month <= 6 else filing_date.year
    return previous_business_day(date(year, 6, 30)), "calendar_year_second_quarter_fallback"


def parse_named_month_date(raw: str) -> date | None:
    clean = str(raw or "").replace(".", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None


def parse_public_float_disclosure(text: str, *, filing_date: date) -> dict[str, Any] | None:
    """Extract SEC 10-K public-float dollars from filing text.

    Returns dollar public float plus a measurement date when available.  The
    dollar value is later divided by a point-in-time market close to estimate
    float shares.  This is intentionally marked as a proxy, not a direct float
    shares feed.
    """
    if not text:
        return None
    text = text[:150_000]
    candidates: list[dict[str, Any]] = []
    for match in ENTITY_PUBLIC_FLOAT_RE.finditer(text):
        value = scaled_money_value(match.group("value"))
        if value is None:
            continue
        start = max(0, match.start() - 700)
        end = min(len(text), match.end() + 700)
        context = text[start:end]
        candidates.append(
            {
                "public_float_usd": value,
                "measurement_date": None,
                "measurement_date_source": "entity_public_float_tag",
                "confidence": 0.95,
                "context": context,
            }
        )
    for regex, source, confidence in (
        (AGGREGATE_MARKET_VALUE_RE, "aggregate_market_value_non_affiliates_text", 0.85),
        (PUBLIC_FLOAT_CONTEXT_RE, "public_float_text", 0.70),
    ):
        for match in regex.finditer(text):
            value = scaled_money_value(match.group("value"), match.groupdict().get("scale", ""))
            if value is None:
                continue
            start = max(0, match.start() - 500)
            end = min(len(text), match.end() + 500)
            context = text[start:end]
            measurement_date = None
            date_match = AS_OF_DATE_RE.search(context)
            if date_match:
                measurement_date = parse_named_month_date(date_match.group("date"))
            candidates.append(
                {
                    "public_float_usd": value,
                    "measurement_date": measurement_date,
                    "measurement_date_source": "parsed_cover_page_date" if measurement_date else source,
                    "confidence": confidence,
                    "context": context,
                }
            )
    filtered = [
        item
        for item in candidates
        if 1_000_000.0 <= float(item["public_float_usd"]) <= 5_000_000_000_000.0
    ]
    if not filtered:
        return None
    best = sorted(filtered, key=lambda item: (float(item["confidence"]), float(item["public_float_usd"])), reverse=True)[0]
    if best.get("measurement_date") is None:
        measurement_date, fallback_source = infer_public_float_measurement_date(filing_date)
        best["measurement_date"] = measurement_date
        best["measurement_date_source"] = f"{best['measurement_date_source']}|{fallback_source}"
    return best


def latest_close_on_or_before(
    biotech_conn: sqlite3.Connection,
    *,
    ticker: str,
    target_date: date,
) -> tuple[date, float] | None:
    row = biotech_conn.execute(
        """
        SELECT bar_date, close
        FROM market_bars_daily
        WHERE ticker = ?
          AND bar_date <= ?
          AND close IS NOT NULL
          AND close > 0
        ORDER BY bar_date DESC
        LIMIT 1
        """,
        (ticker, target_date.isoformat()),
    ).fetchone()
    if not row:
        return None
    price_date = parse_date(row["bar_date"])
    close_price = to_float(row["close"])
    if price_date is None or close_price is None or close_price <= 0.0:
        return None
    return price_date, close_price


def ingest_sec_public_float_proxies(
    conn: sqlite3.Connection,
    biotech_db_path: Path,
    *,
    history_start_date: date = DEFAULT_HISTORY_START_DATE,
    end_date: date | None = None,
    tickers: set[str] | None = None,
    max_filings_per_ticker: int = 9,
    forms: tuple[str, ...] = SEC_PUBLIC_FLOAT_FORMS,
) -> int:
    """Extract SEC 10-K public-float dollars and convert to float-share proxies."""
    end_date = end_date or datetime.now(timezone.utc).date()
    min_filing_date = history_start_date - timedelta(days=550)
    tickers = {normalize_ticker(ticker) for ticker in (tickers or set()) if normalize_ticker(ticker)}
    form_placeholders = ",".join("?" for _ in forms)
    records: list[tuple[Any, ...]] = []
    now = utc_now()
    with sqlite3.connect(str(biotech_db_path)) as biotech_conn:
        biotech_conn.row_factory = sqlite3.Row
        if not tickers:
            tickers = {
                normalize_ticker(row["ticker"])
                for row in biotech_conn.execute("SELECT ticker FROM companies WHERE ticker IS NOT NULL")
                if normalize_ticker(row["ticker"])
            }
        query = f"""
            SELECT c.ticker, f.company_id, f.form, f.filing_date, f.accession_nodash,
                   d.document_type, d.text_content
            FROM sec_filings f
            JOIN companies c ON c.company_id = f.company_id
            JOIN sec_filing_latest_document ld ON ld.accession_nodash = f.accession_nodash
            JOIN sec_filing_documents d ON d.document_id = ld.document_id
            WHERE c.ticker = ?
              AND f.form IN ({form_placeholders})
              AND f.filing_date >= ?
              AND f.filing_date <= ?
              AND d.text_content IS NOT NULL
              AND length(d.text_content) > 1000
            ORDER BY f.filing_date DESC
            LIMIT ?
        """
        for ticker in sorted(tickers):
            params: list[Any] = [
                ticker,
                *forms,
                min_filing_date.isoformat(),
                end_date.isoformat(),
                max(1, int(max_filings_per_ticker)),
            ]
            for row in biotech_conn.execute(query, params):
                filing_date = parse_date(row["filing_date"])
                if filing_date is None:
                    continue
                parsed = parse_public_float_disclosure(str(row["text_content"] or ""), filing_date=filing_date)
                if not parsed:
                    continue
                measurement_date = parsed["measurement_date"]
                if not isinstance(measurement_date, date):
                    continue
                close = latest_close_on_or_before(biotech_conn, ticker=ticker, target_date=measurement_date)
                if close is None:
                    continue
                price_date, close_price = close
                public_float_usd = float(parsed["public_float_usd"])
                float_shares = public_float_usd / close_price
                if not math.isfinite(float_shares) or float_shares <= 0.0:
                    continue
                records.append(
                    (
                        ticker,
                        filing_date.isoformat(),
                        float_shares,
                        SEC_PUBLIC_FLOAT_SOURCE,
                        str(biotech_db_path),
                        measurement_date.isoformat(),
                        filing_date.isoformat(),
                        str(row["accession_nodash"] or ""),
                        public_float_usd,
                        measurement_date.isoformat(),
                        close_price,
                        price_date.isoformat(),
                        1.0,
                        now,
                        now,
                    )
                )
    with conn:
        conn.executemany(
            """
            INSERT INTO float_shares_snapshots(
                ticker, asof_date, float_shares, source, source_file,
                source_asof_date, source_filing_date, source_accession_nodash,
                public_float_usd, public_float_measurement_date, close_price, price_date,
                proxy_flag, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                float_shares = excluded.float_shares,
                source_file = excluded.source_file,
                source_asof_date = excluded.source_asof_date,
                source_filing_date = excluded.source_filing_date,
                source_accession_nodash = excluded.source_accession_nodash,
                public_float_usd = excluded.public_float_usd,
                public_float_measurement_date = excluded.public_float_measurement_date,
                close_price = excluded.close_price,
                price_date = excluded.price_date,
                proxy_flag = excluded.proxy_flag,
                updated_at = excluded.updated_at
            """,
            records,
        )
    update_feed_state(
        conn,
        feed_name="sec_public_float_proxy",
        history_start_date=history_start_date,
        source=SEC_PUBLIC_FLOAT_SOURCE,
        source_file=biotech_db_path,
        row_count=len(records),
        message=f"SEC public-float proxies extracted from 10-K filings rows={len(records)}",
    )
    return len(records)


def backfill_short_interest_float_shares(conn: sqlite3.Connection) -> int:
    """Enrich short-interest rows with point-in-time float shares when available."""
    before = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM short_interest_snapshots
            WHERE COALESCE(float_shares, 0.0) <= 0.0
               OR COALESCE(short_interest_pct_float, 0.0) <= 0.0
            """
        ).fetchone()[0]
    )
    with conn:
        conn.execute(
            """
            UPDATE short_interest_snapshots
            SET
                float_shares = (
                    SELECT f.float_shares
                    FROM float_shares_snapshots f
                    WHERE f.ticker = short_interest_snapshots.ticker
                      AND f.asof_date <= short_interest_snapshots.asof_date
                    ORDER BY
                        f.asof_date DESC,
                        CASE WHEN COALESCE(f.proxy_flag, 0.0) <= 0.0 THEN 0 ELSE 1 END ASC,
                        f.updated_at DESC
                    LIMIT 1
                ),
                short_interest_pct_float = CASE
                    WHEN COALESCE(short_interest_shares, 0.0) > 0.0
                     AND (
                        SELECT f.float_shares
                        FROM float_shares_snapshots f
                        WHERE f.ticker = short_interest_snapshots.ticker
                          AND f.asof_date <= short_interest_snapshots.asof_date
                        ORDER BY
                            f.asof_date DESC,
                            CASE WHEN COALESCE(f.proxy_flag, 0.0) <= 0.0 THEN 0 ELSE 1 END ASC,
                            f.updated_at DESC
                        LIMIT 1
                     ) > 0.0
                    THEN short_interest_shares / (
                        SELECT f.float_shares
                        FROM float_shares_snapshots f
                        WHERE f.ticker = short_interest_snapshots.ticker
                          AND f.asof_date <= short_interest_snapshots.asof_date
                        ORDER BY
                            f.asof_date DESC,
                            CASE WHEN COALESCE(f.proxy_flag, 0.0) <= 0.0 THEN 0 ELSE 1 END ASC,
                            f.updated_at DESC
                        LIMIT 1
                    )
                    ELSE short_interest_pct_float
                END,
                updated_at = ?
            WHERE EXISTS (
                SELECT 1
                FROM float_shares_snapshots f
                WHERE f.ticker = short_interest_snapshots.ticker
                  AND f.asof_date <= short_interest_snapshots.asof_date
            )
            """,
            (utc_now(),),
        )
    after = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM short_interest_snapshots
            WHERE COALESCE(float_shares, 0.0) <= 0.0
               OR COALESCE(short_interest_pct_float, 0.0) <= 0.0
            """
        ).fetchone()[0]
    )
    return max(0, before - after)


def ingest_company_fact_share_proxies(
    conn: sqlite3.Connection,
    *,
    biotech_db_path: Path,
    history_start_date: date,
    end_date: date,
    tickers: set[str] | None = None,
) -> int:
    """Load quarterly shares-outstanding snapshots as a float-share proxy.

    SEC public-float text is usually annual and stale.  Quarterly
    shares_outstanding is not true free float, but it is a better point-in-time
    denominator than leaving short_interest_pct_float blank.  The proxy_flag and
    source label keep the distinction visible to validation and reports.
    """
    if not biotech_db_path.exists():
        raise FileNotFoundError(biotech_db_path)
    ticker_filter = {normalize_ticker(ticker) for ticker in (tickers or set()) if normalize_ticker(ticker)}
    source = "sec_company_facts_shares_outstanding_proxy"
    now = utc_now()
    rows_to_upsert: list[tuple[Any, ...]] = []
    biotech_conn = sqlite3.connect(f"file:{biotech_db_path.as_posix()}?mode=ro", uri=True)
    biotech_conn.row_factory = sqlite3.Row
    try:
        rows = biotech_conn.execute(
            """
            SELECT c.ticker, q.period_end, q.filed_date, q.accession_nodash,
                   q.shares_outstanding, q.shares_source_concept
            FROM company_facts_quarterly q
            JOIN companies c ON c.company_id = q.company_id
            WHERE q.period_end >= ?
              AND q.period_end <= ?
              AND COALESCE(q.shares_outstanding, 0.0) > 0.0
            ORDER BY c.ticker, q.period_end
            """,
            (history_start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    finally:
        biotech_conn.close()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker or (ticker_filter and ticker not in ticker_filter):
            continue
        shares = to_float(row["shares_outstanding"])
        if shares is None or shares <= 0.0:
            continue
        asof = parse_date(row["filed_date"]) or parse_date(row["period_end"])
        source_asof = parse_date(row["period_end"])
        if asof is None or source_asof is None or asof < history_start_date or asof > end_date:
            continue
        rows_to_upsert.append(
            (
                ticker,
                asof.isoformat(),
                shares,
                source,
                "",
                source_asof.isoformat(),
                str(row["filed_date"] or ""),
                str(row["accession_nodash"] or ""),
                None,
                source_asof.isoformat(),
                None,
                "",
                1.0,
                now,
                now,
            )
        )
    with conn:
        conn.executemany(
            """
            INSERT INTO float_shares_snapshots(
                ticker, asof_date, float_shares, source, source_file, source_asof_date,
                source_filing_date, source_accession_nodash, public_float_usd,
                public_float_measurement_date, close_price, price_date, proxy_flag,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                float_shares = excluded.float_shares,
                source_asof_date = excluded.source_asof_date,
                source_filing_date = excluded.source_filing_date,
                source_accession_nodash = excluded.source_accession_nodash,
                public_float_measurement_date = excluded.public_float_measurement_date,
                proxy_flag = excluded.proxy_flag,
                updated_at = excluded.updated_at
            """,
            rows_to_upsert,
        )
    update_feed_state(
        conn,
        feed_name="company_fact_share_proxy",
        history_start_date=history_start_date,
        source=source,
        source_file=biotech_db_path,
        row_count=len(rows_to_upsert),
        message=f"Quarterly company-facts shares-outstanding proxies rows={len(rows_to_upsert)}",
    )
    return len(rows_to_upsert)


def filing_key_for(row: dict[str, Any], *, source: str) -> str:
    accession = str(first_present(row, "accession_number", "accession", "filing_accession")).strip()
    if accession:
        return accession
    manager_cik = str(first_present(row, "manager_cik", "cik")).strip()
    manager_name = str(first_present(row, "manager_name", "manager")).strip()
    period = str(first_present(row, "period_of_report", "report_period", "quarter_end")).strip()
    filing = str(first_present(row, "filing_date", "accepted_at", "asof_date")).strip()
    return "|".join([source, manager_cik or manager_name, period, filing])


def ingest_13f_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    history_start_date: date = DEFAULT_HISTORY_START_DATE,
    source: str = "csv",
) -> tuple[int, int]:
    rows = read_csv_rows(csv_path)
    now = utc_now()
    filing_rows: dict[str, tuple[Any, ...]] = {}
    holding_rows: list[tuple[Any, ...]] = []
    direct_snapshot_rows: list[tuple[Any, ...]] = []
    for row in rows:
        ticker = normalize_ticker(first_present(row, "ticker", "symbol"))
        if not ticker:
            continue
        asof = parse_date(first_present(row, "asof_date", "filing_date", "accepted_at", "publication_date"))
        period = parse_date(first_present(row, "period_of_report", "report_period", "quarter_end"))
        if asof is None or asof < history_start_date:
            continue
        direct_delta = normalize_pct(first_present(row, "institutional_ownership_delta_pct", "ownership_delta_pct", "13f_ownership_delta_pct"))
        manager_cik = str(first_present(row, "manager_cik", "cik")).strip()
        manager_name = str(first_present(row, "manager_name", "manager")).strip()
        shares = to_float(first_present(row, "shares", "shares_held", "institutional_shares"))
        market_value = to_float(first_present(row, "market_value", "value", "institutional_value"))
        if direct_delta is not None and not manager_cik and not manager_name:
            direct_snapshot_rows.append(
                (
                    ticker,
                    asof.isoformat(),
                    period.isoformat() if period else "",
                    shares,
                    market_value,
                    int(to_float(first_present(row, "manager_count"), 0.0) or 0),
                    int(to_float(first_present(row, "new_buyer_count"), 0.0) or 0),
                    int(to_float(first_present(row, "exiting_holder_count"), 0.0) or 0),
                    int(to_float(first_present(row, "net_buyer_count"), 0.0) or 0),
                    direct_delta,
                    str(first_present(row, "source") or source),
                    now,
                    now,
                )
            )
            continue
        filing_key = filing_key_for(row, source=source)
        filing_rows[filing_key] = (
            filing_key,
            str(first_present(row, "accession_number", "accession", "filing_accession")),
            manager_cik,
            manager_name,
            period.isoformat() if period else "",
            asof.isoformat(),
            str(first_present(row, "accepted_at")) or asof.isoformat(),
            str(first_present(row, "source") or source),
            str(csv_path),
            now,
            now,
        )
        holding_rows.append(
            (
                filing_key,
                manager_cik,
                manager_name,
                ticker,
                str(first_present(row, "cusip")),
                period.isoformat() if period else "",
                asof.isoformat(),
                str(first_present(row, "accepted_at")) or asof.isoformat(),
                shares,
                market_value,
                str(first_present(row, "title_of_class", "TITLEOFCLASS", "titleOfClass") or ""),
                str(first_present(row, "share_type", "SSHPRNAMTTYPE", "sshPrnamtType") or ""),
                str(first_present(row, "put_call", "PUTCALL", "putCall") or ""),
                str(first_present(row, "source") or source),
                str(csv_path),
                now,
                now,
            )
        )
    with conn:
        conn.executemany(
            """
            INSERT INTO institutional_13f_filings(
                filing_key, accession_number, manager_cik, manager_name, period_of_report,
                filing_date, accepted_at, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key) DO UPDATE SET
                accession_number = excluded.accession_number,
                manager_cik = excluded.manager_cik,
                manager_name = excluded.manager_name,
                period_of_report = excluded.period_of_report,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            list(filing_rows.values()),
        )
        conn.executemany(
            """
            INSERT INTO institutional_13f_holdings(
                filing_key, manager_cik, manager_name, ticker, cusip, period_of_report,
                filing_date, accepted_at, shares, market_value, title_of_class, share_type, put_call,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key, ticker, cusip) DO UPDATE SET
                shares = excluded.shares,
                market_value = excluded.market_value,
                title_of_class = excluded.title_of_class,
                share_type = excluded.share_type,
                put_call = excluded.put_call,
                updated_at = excluded.updated_at
            """,
            holding_rows,
        )
        conn.executemany(
            """
            INSERT INTO institutional_13f_ownership_snapshots(
                ticker, asof_date, period_of_report, institutional_shares, institutional_value,
                manager_count, new_buyer_count, exiting_holder_count, net_buyer_count,
                institutional_ownership_delta_pct, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                period_of_report = excluded.period_of_report,
                institutional_shares = excluded.institutional_shares,
                institutional_value = excluded.institutional_value,
                manager_count = excluded.manager_count,
                new_buyer_count = excluded.new_buyer_count,
                exiting_holder_count = excluded.exiting_holder_count,
                net_buyer_count = excluded.net_buyer_count,
                institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
                updated_at = excluded.updated_at
            """,
            direct_snapshot_rows,
        )
    aggregate_13f_ownership(conn, source=source)
    update_feed_state(
        conn,
        feed_name="institutional_13f",
        history_start_date=history_start_date,
        source=source,
        source_file=csv_path,
        row_count=len(holding_rows) + len(direct_snapshot_rows),
    )
    return len(filing_rows), len(holding_rows) + len(direct_snapshot_rows)


def aggregate_13f_ownership(conn: sqlite3.Connection, *, source: str = "csv") -> int:
    rows = conn.execute(
        """
        SELECT ticker, filing_date AS asof_date, period_of_report,
               COALESCE(NULLIF(manager_cik, ''), NULLIF(manager_name, ''), filing_key) AS manager_key,
               COALESCE(shares, 0.0) AS shares,
               COALESCE(market_value, 0.0) AS market_value
        FROM institutional_13f_holdings
        WHERE UPPER(COALESCE(share_type, '')) IN ('', 'SH')
          AND COALESCE(put_call, '') = ''
        ORDER BY ticker, filing_date
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["ticker"]), str(row["asof_date"]), str(row["period_of_report"] or ""))
        bucket = grouped.setdefault(
            key,
            {
                "institutional_shares": 0.0,
                "institutional_value": 0.0,
                "managers": set(),
            },
        )
        bucket["institutional_shares"] += to_float(row["shares"], 0.0) or 0.0
        bucket["institutional_value"] += to_float(row["market_value"], 0.0) or 0.0
        manager_key = str(row["manager_key"] or "").strip()
        if manager_key:
            bucket["managers"].add(manager_key)

    by_ticker: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    for (ticker, asof_date, period), payload in grouped.items():
        by_ticker[ticker].append((asof_date, period, payload))
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    for ticker, ticker_rows in by_ticker.items():
        prior_shares: float | None = None
        prior_managers: set[str] | None = None
        for asof_date, period, payload in sorted(ticker_rows, key=lambda item: item[0]):
            shares = to_float(payload["institutional_shares"], 0.0) or 0.0
            delta = (shares - prior_shares) / prior_shares if prior_shares and prior_shares > 0.0 else None
            prior_shares = shares
            managers = set(payload.get("managers") or set())
            if prior_managers is None:
                new_buyer_count = 0
                exiting_holder_count = 0
            else:
                new_buyer_count = len(managers - prior_managers)
                exiting_holder_count = len(prior_managers - managers)
            prior_managers = managers
            records.append(
                (
                    ticker,
                    asof_date,
                    period,
                    shares,
                    payload["institutional_value"],
                    len(managers),
                    new_buyer_count,
                    exiting_holder_count,
                    new_buyer_count - exiting_holder_count,
                    delta,
                    source,
                    now,
                    now,
                )
            )
    with conn:
        conn.executemany(
            """
            INSERT INTO institutional_13f_ownership_snapshots(
                ticker, asof_date, period_of_report, institutional_shares, institutional_value,
                manager_count, new_buyer_count, exiting_holder_count, net_buyer_count,
                institutional_ownership_delta_pct, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                period_of_report = excluded.period_of_report,
                institutional_shares = excluded.institutional_shares,
                institutional_value = excluded.institutional_value,
                manager_count = excluded.manager_count,
                new_buyer_count = excluded.new_buyer_count,
                exiting_holder_count = excluded.exiting_holder_count,
                net_buyer_count = excluded.net_buyer_count,
                institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
                updated_at = excluded.updated_at
            """,
            records,
        )
    return len(records)


def load_tickers(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    return {
        ticker
        for ticker in (normalize_ticker(row.get("ticker") or row.get("symbol")) for row in read_csv_rows(path))
        if ticker
    }


def latest_float_snapshot(conn: sqlite3.Connection, ticker: str, asof_date: date) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM float_shares_snapshots
        WHERE ticker = ?
          AND asof_date <= ?
        ORDER BY
            asof_date DESC,
            CASE WHEN COALESCE(proxy_flag, 0.0) <= 0.0 THEN 0 ELSE 1 END ASC,
            updated_at DESC
        LIMIT 1
        """,
        (ticker, asof_date.isoformat()),
    ).fetchone()
    return dict(row) if row else {}


def latest_short_interest_rows(conn: sqlite3.Connection, asof_date: date, tickers: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT s.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker
                       ORDER BY asof_date DESC,
                                CASE source
                                    WHEN 'finra_equity_short_interest_files' THEN 1
                                    WHEN 'finra_equity_short_interest' THEN 2
                                    ELSE 9
                                END ASC,
                                updated_at DESC
                   ) AS rn
            FROM short_interest_snapshots s
            WHERE asof_date <= ?
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY ticker
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if tickers and item["ticker"] not in tickers:
            continue
        item_asof = parse_date(item.get("asof_date")) or asof_date
        float_snapshot = latest_float_snapshot(conn, str(item["ticker"]), item_asof)
        float_asof = parse_date(float_snapshot.get("asof_date"))
        source_asof = parse_date(float_snapshot.get("source_asof_date") or float_snapshot.get("public_float_measurement_date"))
        out.append(
            {
                "ticker": item["ticker"],
                "asof_date": item["asof_date"],
                "settlement_date": item.get("settlement_date", ""),
                "publication_date": item.get("publication_date", ""),
                "short_interest_shares": item.get("short_interest_shares", 0.0),
                "float_shares": item.get("float_shares", 0.0),
                "short_interest_pct_float": item.get("short_interest_pct_float", 0.0),
                "days_to_cover": item.get("days_to_cover", 0.0),
                "float_shares_source": float_snapshot.get("source", ""),
                "float_shares_asof_date": float_snapshot.get("asof_date", ""),
                "float_shares_source_asof_date": float_snapshot.get("source_asof_date", ""),
                "float_shares_staleness_days": (
                    "" if float_asof is None else max(0, (item_asof - float_asof).days)
                ),
                "float_shares_measurement_staleness_days": (
                    "" if source_asof is None else max(0, (item_asof - source_asof).days)
                ),
                "float_shares_proxy_flag": float_snapshot.get("proxy_flag", 0.0),
                "public_float_usd": float_snapshot.get("public_float_usd", ""),
                "public_float_price_date": float_snapshot.get("price_date", ""),
                "public_float_close_price": float_snapshot.get("close_price", ""),
                "source": item.get("source", ""),
            }
        )
    return out


def latest_13f_rows(conn: sqlite3.Connection, asof_date: date, tickers: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT o.*
        FROM institutional_13f_ownership_snapshots o
        JOIN (
            SELECT ticker, MAX(asof_date) AS max_asof
            FROM institutional_13f_ownership_snapshots
            WHERE asof_date <= ?
            GROUP BY ticker
        ) latest
          ON latest.ticker = o.ticker AND latest.max_asof = o.asof_date
        ORDER BY o.ticker
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if tickers and item["ticker"] not in tickers:
            continue
        out.append(
            {
                "ticker": item["ticker"],
                "asof_date": item["asof_date"],
                "period_of_report": item.get("period_of_report", ""),
                "institutional_shares": item.get("institutional_shares", 0.0),
                "institutional_value": item.get("institutional_value", 0.0),
                "manager_count": item.get("manager_count", 0),
                "new_buyer_count": item.get("new_buyer_count", 0),
                "exiting_holder_count": item.get("exiting_holder_count", 0),
                "net_buyer_count": item.get("net_buyer_count", 0),
                "institutional_ownership_delta_pct": item.get("institutional_ownership_delta_pct", 0.0),
                "source": item.get("source", ""),
            }
        )
    return out


def borrow_cost_pressure_score(rate: float | None, *, hard_to_borrow: bool = False) -> float:
    """Score borrow-cost pressure where higher means tighter/more expensive borrow."""
    if rate is None or not math.isfinite(rate) or rate <= 0.0:
        rate_score = 0.0
    else:
        rate_score = linear_interpolated_score(
            rate,
            [
                (0.0, 0.0),
                (0.01, 5.0),
                (0.05, 25.0),
                (0.15, 55.0),
                (0.50, 85.0),
                (1.00, 100.0),
            ],
        )
    supply_score = 100.0 if hard_to_borrow else 0.0
    return max(0.0, min(100.0, 0.75 * rate_score + 0.25 * supply_score))


def linear_interpolated_score(value: float, points: list[tuple[float, float]]) -> float:
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return max(0.0, min(100.0, ordered[0][1]))
    if value >= ordered[-1][0]:
        return max(0.0, min(100.0, ordered[-1][1]))
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            span = max(1e-12, right_x - left_x)
            return max(0.0, min(100.0, left_y + (right_y - left_y) * (value - left_x) / span))
    return max(0.0, min(100.0, ordered[-1][1]))


def latest_borrow_availability_rows(
    conn: sqlite3.Connection,
    asof_date: date,
    tickers: set[str],
    *,
    max_fee_staleness_days: int = 10,
    max_snapshot_staleness_days: int = 7,
    hard_to_borrow_shares: float = 50_000.0,
) -> list[dict[str, Any]]:
    cutoff_90 = (asof_date - timedelta(days=90)).isoformat()
    cutoff_30 = (asof_date - timedelta(days=30)).isoformat()
    max_fee_staleness_days = max(0, int(max_fee_staleness_days))
    max_snapshot_staleness_days = max(0, int(max_snapshot_staleness_days))
    hard_to_borrow_shares = max(0.0, float(hard_to_borrow_shares))
    latest_fee_rows = conn.execute(
        """
        SELECT f.*
        FROM ibkr_borrow_fee_rate_daily f
        JOIN (
            SELECT ticker, MAX(asof_date) AS max_asof
            FROM ibkr_borrow_fee_rate_daily
            WHERE asof_date <= ?
            GROUP BY ticker
        ) latest
          ON latest.ticker = f.ticker AND latest.max_asof = f.asof_date
        ORDER BY f.ticker
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    fee_history_rows = conn.execute(
        """
        SELECT ticker, asof_date, borrow_fee_rate
        FROM ibkr_borrow_fee_rate_daily
        WHERE asof_date <= ? AND asof_date >= ?
        ORDER BY ticker, asof_date
        """,
        (asof_date.isoformat(), cutoff_90),
    ).fetchall()
    latest_shortable_rows = conn.execute(
        """
        SELECT s.*
        FROM ibkr_shortable_shares_snapshots s
        JOIN (
            SELECT ticker, MAX(asof_date) AS max_asof
            FROM ibkr_shortable_shares_snapshots
            WHERE asof_date <= ?
            GROUP BY ticker
        ) latest
          ON latest.ticker = s.ticker AND latest.max_asof = s.asof_date
        ORDER BY s.ticker
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    fee_by_ticker = {str(row["ticker"]): dict(row) for row in latest_fee_rows}
    shortable_by_ticker = {str(row["ticker"]): dict(row) for row in latest_shortable_rows}
    history_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fee_history_rows:
        item = dict(row)
        history_by_ticker[str(item["ticker"])].append(item)

    selected_tickers = sorted((tickers or set(fee_by_ticker) | set(shortable_by_ticker)) & (set(fee_by_ticker) | set(shortable_by_ticker)))
    out: list[dict[str, Any]] = []
    for ticker in selected_tickers:
        latest_fee = fee_by_ticker.get(ticker, {})
        latest_shortable = shortable_by_ticker.get(ticker, {})
        fee_asof = parse_date(latest_fee.get("asof_date"))
        shortable_asof = parse_date(latest_shortable.get("asof_date"))
        fee_is_current = fee_asof is not None and (asof_date - fee_asof).days <= max_fee_staleness_days
        shortable_is_current = (
            shortable_asof is not None and (asof_date - shortable_asof).days <= max_snapshot_staleness_days
        )
        fee_staleness_days = "" if fee_asof is None else max(0, (asof_date - fee_asof).days)
        shortable_staleness_days = "" if shortable_asof is None else max(0, (asof_date - shortable_asof).days)
        current_rate = to_float(latest_fee.get("borrow_fee_rate")) if fee_is_current else None
        history = history_by_ticker.get(ticker, [])
        rates_90 = [
            to_float(row.get("borrow_fee_rate"))
            for row in history
            if to_float(row.get("borrow_fee_rate")) is not None
        ]
        rates_30 = [
            rate
            for row, rate in (
                (row, to_float(row.get("borrow_fee_rate")))
                for row in history
            )
            if rate is not None and str(row.get("asof_date") or "") >= cutoff_30
        ]
        avg_30 = sum(rates_30) / len(rates_30) if rates_30 else None
        avg_90 = sum(rates_90) / len(rates_90) if rates_90 else None
        peak_90 = max(rates_90) if rates_90 else None
        shortable_shares = to_float(latest_shortable.get("shortable_shares")) if shortable_is_current else None
        hard_to_borrow = shortable_shares is not None and shortable_shares < hard_to_borrow_shares
        spike = (
            current_rate is not None
            and avg_90 is not None
            and current_rate >= 0.05
            and current_rate >= max(avg_90 * 3.0, avg_90 + 0.05)
        )
        declining = (
            current_rate is not None
            and peak_90 is not None
            and peak_90 >= 0.15
            and current_rate <= peak_90 * 0.50
        )
        out.append(
            {
                "ticker": ticker,
                "asof_date": asof_date.isoformat(),
                "borrow_fee_asof_date": fee_asof.isoformat() if fee_asof is not None else "",
                "shortable_asof_date": shortable_asof.isoformat() if shortable_asof is not None else "",
                "borrow_fee_data_available_flag": 1.0 if current_rate is not None else 0.0,
                "shortable_data_available_flag": 1.0 if shortable_shares is not None else 0.0,
                "borrow_fee_stale_flag": 0.0 if fee_is_current else 1.0,
                "shortable_stale_flag": 0.0 if shortable_is_current else 1.0,
                "borrow_fee_staleness_days": fee_staleness_days,
                "shortable_staleness_days": shortable_staleness_days,
                "borrow_fee_history_count_30d": len(rates_30),
                "borrow_fee_history_count_90d": len(rates_90),
                "borrow_rate_current": "" if current_rate is None else round(current_rate, 8),
                "borrow_rate_30d_avg": "" if avg_30 is None else round(avg_30, 8),
                "borrow_rate_90d_avg": "" if avg_90 is None else round(avg_90, 8),
                "borrow_rate_90d_peak": "" if peak_90 is None else round(peak_90, 8),
                "borrow_rate_spike_flag": 1.0 if spike else 0.0,
                "borrow_rate_declining_flag": 1.0 if declining else 0.0,
                "shortable_shares": "" if shortable_shares is None else round(shortable_shares, 4),
                "shares_shortable_k": "" if shortable_shares is None else round(shortable_shares / 1000.0, 4),
                "hard_to_borrow_flag": 1.0 if hard_to_borrow else 0.0,
                "borrow_pressure_score": round(borrow_cost_pressure_score(current_rate, hard_to_borrow=hard_to_borrow), 4),
                "fee_rate_source": latest_fee.get("source", ""),
                "shortable_source": latest_shortable.get("source", ""),
            }
        )
    return out


def export_positioning_features(
    conn: sqlite3.Connection,
    *,
    asof_date: date,
    output_dir: Path,
    tickers_csv: Path | None = None,
    max_borrow_fee_staleness_days: int = 10,
    max_borrow_snapshot_staleness_days: int = 7,
    hard_to_borrow_shares: float = 50_000.0,
) -> tuple[Path, Path, Path, int, int, int]:
    tickers = load_tickers(tickers_csv)
    short_rows = latest_short_interest_rows(conn, asof_date, tickers)
    institutional_rows = latest_13f_rows(conn, asof_date, tickers)
    borrow_rows = latest_borrow_availability_rows(
        conn,
        asof_date,
        tickers,
        max_fee_staleness_days=max_borrow_fee_staleness_days,
        max_snapshot_staleness_days=max_borrow_snapshot_staleness_days,
        hard_to_borrow_shares=hard_to_borrow_shares,
    )
    short_path = output_dir / "short_interest_features.csv"
    institutional_path = output_dir / "institutional_ownership_features.csv"
    borrow_path = output_dir / "borrow_availability_features.csv"
    write_csv_rows(
        short_path,
        [
            "ticker",
            "asof_date",
            "settlement_date",
            "publication_date",
            "short_interest_shares",
            "float_shares",
            "short_interest_pct_float",
            "days_to_cover",
            "float_shares_source",
            "float_shares_asof_date",
            "float_shares_source_asof_date",
            "float_shares_staleness_days",
            "float_shares_measurement_staleness_days",
            "float_shares_proxy_flag",
            "public_float_usd",
            "public_float_price_date",
            "public_float_close_price",
            "source",
        ],
        short_rows,
    )
    write_csv_rows(
        institutional_path,
        [
            "ticker",
            "asof_date",
            "period_of_report",
            "institutional_shares",
            "institutional_value",
            "manager_count",
            "new_buyer_count",
            "exiting_holder_count",
            "net_buyer_count",
            "institutional_ownership_delta_pct",
            "source",
        ],
        institutional_rows,
    )
    write_csv_rows(
        borrow_path,
        [
            "ticker",
            "asof_date",
            "borrow_fee_asof_date",
            "shortable_asof_date",
            "borrow_fee_data_available_flag",
            "shortable_data_available_flag",
            "borrow_fee_stale_flag",
            "shortable_stale_flag",
            "borrow_fee_staleness_days",
            "shortable_staleness_days",
            "borrow_fee_history_count_30d",
            "borrow_fee_history_count_90d",
            "borrow_rate_current",
            "borrow_rate_30d_avg",
            "borrow_rate_90d_avg",
            "borrow_rate_90d_peak",
            "borrow_rate_spike_flag",
            "borrow_rate_declining_flag",
            "shortable_shares",
            "shares_shortable_k",
            "hard_to_borrow_flag",
            "borrow_pressure_score",
            "fee_rate_source",
            "shortable_source",
        ],
        borrow_rows,
    )
    return short_path, institutional_path, borrow_path, len(short_rows), len(institutional_rows), len(borrow_rows)


def add_common_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)


def parse_history_start(raw: str) -> date:
    parsed = parse_date(raw)
    if parsed is None:
        raise ValueError(f"Invalid history start date: {raw!r}")
    return parsed
