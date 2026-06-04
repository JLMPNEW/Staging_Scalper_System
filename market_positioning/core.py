from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\market_positioning.sqlite")
DEFAULT_HISTORY_START_DATE = date(2019, 1, 1)


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
            return datetime.strptime(text[: max(10, len(text))], fmt).date()
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
                institutional_ownership_delta_pct REAL,
                source TEXT NOT NULL DEFAULT 'csv',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, asof_date, source)
            );

            CREATE INDEX IF NOT EXISTS idx_13f_ownership_ticker_asof
                ON institutional_13f_ownership_snapshots(ticker, asof_date);

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
                filing_date, accepted_at, shares, market_value, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key, ticker, cusip) DO UPDATE SET
                shares = excluded.shares,
                market_value = excluded.market_value,
                updated_at = excluded.updated_at
            """,
            holding_rows,
        )
        conn.executemany(
            """
            INSERT INTO institutional_13f_ownership_snapshots(
                ticker, asof_date, period_of_report, institutional_shares, institutional_value,
                manager_count, institutional_ownership_delta_pct, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                period_of_report = excluded.period_of_report,
                institutional_shares = excluded.institutional_shares,
                institutional_value = excluded.institutional_value,
                manager_count = excluded.manager_count,
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
               SUM(COALESCE(shares, 0.0)) AS institutional_shares,
               SUM(COALESCE(market_value, 0.0)) AS institutional_value,
               COUNT(DISTINCT COALESCE(NULLIF(manager_cik, ''), manager_name)) AS manager_count
        FROM institutional_13f_holdings
        GROUP BY ticker, filing_date, period_of_report
        ORDER BY ticker, filing_date
        """
    ).fetchall()
    by_ticker: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row["ticker"])].append(row)
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    for ticker, ticker_rows in by_ticker.items():
        prior_shares: float | None = None
        for row in ticker_rows:
            shares = to_float(row["institutional_shares"], 0.0) or 0.0
            delta = (shares - prior_shares) / prior_shares if prior_shares and prior_shares > 0.0 else 0.0
            prior_shares = shares
            records.append(
                (
                    ticker,
                    row["asof_date"],
                    row["period_of_report"],
                    shares,
                    row["institutional_value"],
                    row["manager_count"],
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
                manager_count, institutional_ownership_delta_pct, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                period_of_report = excluded.period_of_report,
                institutional_shares = excluded.institutional_shares,
                institutional_value = excluded.institutional_value,
                manager_count = excluded.manager_count,
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
        out.append(
            {
                "ticker": item["ticker"],
                "asof_date": item["asof_date"],
                "settlement_date": item.get("settlement_date", ""),
                "publication_date": item.get("publication_date", ""),
                "short_interest_shares": item.get("short_interest_shares", 0.0),
                "short_interest_pct_float": item.get("short_interest_pct_float", 0.0),
                "days_to_cover": item.get("days_to_cover", 0.0),
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
                "institutional_ownership_delta_pct": item.get("institutional_ownership_delta_pct", 0.0),
                "source": item.get("source", ""),
            }
        )
    return out


def export_positioning_features(
    conn: sqlite3.Connection,
    *,
    asof_date: date,
    output_dir: Path,
    tickers_csv: Path | None = None,
) -> tuple[Path, Path, int, int]:
    tickers = load_tickers(tickers_csv)
    short_rows = latest_short_interest_rows(conn, asof_date, tickers)
    institutional_rows = latest_13f_rows(conn, asof_date, tickers)
    short_path = output_dir / "short_interest_features.csv"
    institutional_path = output_dir / "institutional_ownership_features.csv"
    write_csv_rows(
        short_path,
        [
            "ticker",
            "asof_date",
            "settlement_date",
            "publication_date",
            "short_interest_shares",
            "short_interest_pct_float",
            "days_to_cover",
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
            "institutional_ownership_delta_pct",
            "source",
        ],
        institutional_rows,
    )
    return short_path, institutional_path, len(short_rows), len(institutional_rows)


def add_common_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)


def parse_history_start(raw: str) -> date:
    parsed = parse_date(raw)
    if parsed is None:
        raise ValueError(f"Invalid history start date: {raw!r}")
    return parsed
