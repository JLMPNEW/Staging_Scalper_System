#!/usr/bin/env python3
"""Load point-in-time semiconductor research-universe membership rows."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from technology.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("load_semiconductor_historical_membership")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "load_semiconductor_historical_membership"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load semiconductor historical/delisted point-in-time membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--membership-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_date_text(raw: object, *, field: str, ticker: str) -> str:
    text = str(raw or "").strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid {field}={raw!r}; expected YYYY-MM-DD") from exc
    return text


def read_membership_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Historical membership CSV has no header: {path}")
        rows = [{str(key): str(value or "").strip() for key, value in row.items()} for row in reader]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row.get("ticker") or row.get("internal_ticker"))
        if not ticker:
            continue
        if ticker in seen:
            raise ValueError(f"Duplicate historical membership ticker: {ticker}")
        seen.add(ticker)
        exchange_ticker = normalize_ticker(row.get("exchange_ticker")) or ticker
        start_date = parse_date_text(row.get("start_date"), field="start_date", ticker=ticker)
        end_date = parse_date_text(row.get("end_date"), field="end_date", ticker=ticker)
        if end_date < start_date:
            raise ValueError(f"{ticker}: end_date {end_date} precedes start_date {start_date}")
        row["ticker"] = ticker
        row["internal_ticker"] = ticker
        row["exchange_ticker"] = exchange_ticker
        row["cik"] = normalize_cik(row.get("cik"))
        row["start_date"] = start_date
        row["end_date"] = end_date
        row["confidence"] = str(float(row.get("confidence") or 0.75))
        out.append(row)
    if not out:
        raise ValueError(f"No historical membership rows found in {path}")
    return out


def source_id_or_none(conn: Any, source_id: str) -> str | None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return source_id if row is not None else None


def first_price_date(conn: Any, ticker: str, price_source: str) -> str | None:
    row = conn.execute(
        """
        SELECT MIN(bar_date) AS first_date
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ?
          AND adj_close IS NOT NULL
        """,
        (ticker, price_source),
    ).fetchone()
    value = str(row["first_date"] or "") if row is not None else ""
    return value[:10] if value else None


def insert_membership(
    conn: Any,
    *,
    company_id: int,
    ticker: str,
    model_family: str,
    source_id: str,
    basis: str,
    start_date: str,
    end_date: str | None,
    membership_status: str,
    is_current_member: int,
    confidence: float,
    reason: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id, membership_basis,
            start_date, end_date, membership_status, is_current_member,
            point_in_time_flag, confidence, reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, membership_source_id, start_date) DO UPDATE SET
            company_id = excluded.company_id,
            membership_basis = excluded.membership_basis,
            end_date = excluded.end_date,
            membership_status = excluded.membership_status,
            is_current_member = excluded.is_current_member,
            point_in_time_flag = excluded.point_in_time_flag,
            confidence = excluded.confidence,
            reason = excluded.reason,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            ticker,
            model_family,
            source_id,
            basis,
            start_date,
            end_date,
            membership_status,
            int(is_current_member),
            float(confidence),
            reason,
            now,
            now,
        ),
    )


def load_current_pit_rows(
    conn: Any,
    *,
    model_family: str,
    source_id: str,
    price_source: str,
    optimization_start: str,
) -> int:
    rows = conn.execute(
        """
        SELECT c.company_id, c.ticker
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    count = 0
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        first_price = first_price_date(conn, ticker, price_source)
        start_date = max(optimization_start, first_price or optimization_start)
        insert_membership(
            conn,
            company_id=int(row["company_id"]),
            ticker=ticker,
            model_family=model_family,
            source_id=source_id,
            basis="point_in_time_research_universe_current",
            start_date=start_date,
            end_date=None,
            membership_status="active",
            is_current_member=1,
            confidence=0.70,
            reason="Current source-of-truth ticker seeded as PIT research-universe member from first local adjusted-price availability.",
        )
        count += 1
    return count


def upsert_historical_company(conn: Any, row: dict[str, str], *, model_family: str, source_id: str) -> int:
    now = utc_now()
    ticker = normalize_ticker(row["ticker"])
    company_name = str(row.get("company_name") or ticker)
    country = str(row.get("country") or "United States")
    currency = str(row.get("currency") or "USD")
    exchange = str(row.get("exchange") or "")
    security_type = str(row.get("security_type") or "Common Stock")
    cik = normalize_cik(row.get("cik"))
    cohort_id = str(row.get("calibration_cohort_id") or "semi_historical")
    cohort_name = str(row.get("calibration_cohort") or "Historical semiconductor membership")

    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, sector, industry, subsector, country, currency,
            universe_status, is_active, data_quality_status, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, 'Technology', 'Semiconductors', ?, ?, ?, 'historical', 0, 'historical_membership_seed', ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = COALESCE(NULLIF(excluded.cik, ''), dim_company.cik),
            company_name = COALESCE(NULLIF(excluded.company_name, ''), dim_company.company_name),
            sector = COALESCE(NULLIF(dim_company.sector, ''), excluded.sector),
            industry = COALESCE(NULLIF(dim_company.industry, ''), excluded.industry),
            subsector = COALESCE(NULLIF(dim_company.subsector, ''), excluded.subsector),
            country = COALESCE(NULLIF(dim_company.country, ''), excluded.country),
            currency = COALESCE(NULLIF(dim_company.currency, ''), excluded.currency),
            universe_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.universe_status ELSE excluded.universe_status END,
            is_active = CASE WHEN dim_company.is_active = 1 THEN dim_company.is_active ELSE excluded.is_active END,
            data_quality_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.data_quality_status ELSE excluded.data_quality_status END,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            cik,
            company_name,
            str(row.get("notes") or row.get("event_type") or "Historical semiconductor member")[:250],
            country,
            currency,
            now,
            now,
        ),
    )
    company = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    if company is None:
        raise RuntimeError(f"Company upsert failed for historical ticker {ticker}")
    company_id = int(company["company_id"])

    conn.execute(
        """
        INSERT INTO dim_security(
            company_id, ticker, exchange, security_type, listing_status,
            is_primary_listing, currency, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'historical_delisted', 1, ?, ?, ?)
        ON CONFLICT(ticker, exchange) DO UPDATE SET
            company_id = excluded.company_id,
            security_type = excluded.security_type,
            listing_status = excluded.listing_status,
            currency = excluded.currency,
            updated_at = excluded.updated_at
        """,
        (company_id, ticker, exchange, security_type, currency, now, now),
    )
    if cik:
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
            )
            VALUES (?, 'CIK', ?, ?, 0.85, ?, ?)
            ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                company_id = excluded.company_id,
                source_id = excluded.source_id,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (company_id, cik, source_id_or_none(conn, source_id), now, now),
        )
    exchange_ticker = normalize_ticker(row.get("exchange_ticker"))
    if exchange_ticker and exchange_ticker != ticker:
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
            )
            VALUES (?, 'EXCHANGE_TICKER', ?, ?, 0.90, ?, ?)
            ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                company_id = excluded.company_id,
                source_id = excluded.source_id,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (company_id, exchange_ticker, source_id_or_none(conn, source_id), now, now),
        )
    conn.execute(
        """
        INSERT INTO dim_technology_taxonomy(
            company_id, ticker, model_family, sector, subsector, calibration_cohort_id,
            calibration_cohort, subindustry_role, calibration_use, liquidity_instrument_flag,
            taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
        )
        VALUES (?, ?, ?, 'Technology', 'semiconductors', ?, ?, ?, 'historical_backtest',
                'historical_or_delisted', ?, ?, 0, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            company_id = excluded.company_id,
            calibration_cohort_id = excluded.calibration_cohort_id,
            calibration_cohort = excluded.calibration_cohort,
            subindustry_role = excluded.subindustry_role,
            calibration_use = excluded.calibration_use,
            liquidity_instrument_flag = excluded.liquidity_instrument_flag,
            taxonomy_confidence = excluded.taxonomy_confidence,
            taxonomy_source = excluded.taxonomy_source,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            ticker,
            model_family,
            cohort_id,
            cohort_name,
            str(row.get("notes") or row.get("event_type") or "historical_semiconductor")[:250],
            float(row.get("confidence") or 0.75),
            source_id,
            now,
        ),
    )
    return company_id


def load_historical_rows(conn: Any, rows: list[dict[str, str]], *, model_family: str, source_id: str) -> int:
    count = 0
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        company_id = upsert_historical_company(conn, row, model_family=model_family, source_id=source_id)
        reason = "; ".join(
            part
            for part in (
                f"event_type={row.get('event_type')}",
                f"successor={row.get('successor_ticker')}",
                str(row.get("notes") or ""),
                str(row.get("source_url") or ""),
            )
            if part
        )
        insert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            source_id=source_id,
            basis="point_in_time_historical_constituent",
            start_date=row["start_date"],
            end_date=row["end_date"],
            membership_status=str(row.get("membership_status") or "historical"),
            is_current_member=0,
            confidence=float(row.get("confidence") or 0.75),
            reason=reason,
        )
        count += 1
    return count


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    membership_csv = (
        args.membership_csv.expanduser().resolve()
        if args.membership_csv
        else resolve_path(cfg_get(config, "technology_universe.historical_membership_csv"), base_dir=base_dir)
    )
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    source_id = str(cfg_get(config, "technology_universe.historical_membership_source_id", "semiconductor_historical_membership_seed"))
    price_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    optimization_start = parse_date_text(
        cfg_get(config, "technology_universe.optimization_start_date", "2016-01-01"),
        field="optimization_start_date",
        ticker="CONFIG",
    )
    rows = read_membership_csv(membership_csv)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=membership_csv)
        try:
            with conn:
                conn.execute("DELETE FROM dim_universe_membership WHERE membership_source_id = ?", (source_id,))
                current_count = load_current_pit_rows(
                    conn,
                    model_family=model_family,
                    source_id=source_id,
                    price_source=price_source,
                    optimization_start=optimization_start,
                )
                historical_count = load_historical_rows(conn, rows, model_family=model_family, source_id=source_id)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=current_count + historical_count,
                message=f"current_pit={current_count} historical={historical_count} source_id={source_id}",
            )
            LOGGER.info(
                "Loaded semiconductor PIT membership: current=%d historical=%d db=%s",
                current_count,
                historical_count,
                db_path,
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
