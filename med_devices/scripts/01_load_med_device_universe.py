#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import as_bool, normalize_cik, normalize_org_name, normalize_subsector, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("load_med_device_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ACTIVE_LISTING_STATUSES = {"active"}
DEFAULT_NON_INVESTABLE_LISTING_STATUSES = {
    "active_financial_status_d",
    "active_financial_status_e",
    "inactive_or_not_investable",
    "invalid_or_inactive",
}
DEFAULT_INVESTABLE_SECURITY_TYPES = {
    "common_stock",
    "ordinary_shares",
    "adr_ads",
    "american_depositary_shares",
    "new_york_registry_shares",
}


@dataclass(frozen=True)
class UniverseCompany:
    ticker: str
    investability_status: str
    company_name: str
    cik: str
    exchange: str
    sector: str
    industry: str
    subsector: str
    country: str
    currency: str
    security_type: str
    listing_status: str
    is_primary_listing: int
    universe_status: str
    is_active: int
    medtech_pure_play_flag: int
    source_aliases: tuple[str, ...]
    data_quality_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the med-devices ticker universe into the independent DB.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    return parser.parse_args()


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def universe_status_from_flags(row: dict[str, str]) -> str:
    if as_bool(row_get(row, "ManualExclude", "manual_exclude")):
        return "remove"
    if as_bool(row_get(row, "ManualReview", "manual_review")):
        return "review"
    return "keep"


def normalized_set(raw: Any, default: set[str]) -> set[str]:
    values = raw if isinstance(raw, list) else list(default)
    out = {normalize_subsector(value) for value in values if str(value or "").strip()}
    return out or set(default)


def derive_investability_status(
    *,
    manual_status: str,
    listing_status: str,
    security_type: str,
    active_listing_statuses: set[str],
    non_investable_listing_statuses: set[str],
    investable_security_types: set[str],
) -> tuple[str, str, int]:
    if manual_status == "remove":
        return "manual_exclude", "remove", 0
    listing_key = normalize_subsector(listing_status)
    security_key = normalize_subsector(security_type)
    if listing_key in non_investable_listing_statuses:
        return "non_investable_listing_status", "non_investable_listing_status", 0
    if security_key and security_key not in investable_security_types:
        return "non_investable_security_type", "non_investable_security_type", 0
    if listing_key and listing_key not in active_listing_statuses:
        return "review_listing_status", "review", 1
    if manual_status == "review":
        return "manual_review", "review", 1
    return "investable", "keep", 1


def data_quality_status(company: UniverseCompany) -> str:
    required = [
        company.ticker,
        company.company_name,
        company.cik,
        company.exchange,
        company.country,
        company.currency,
        company.security_type,
        company.listing_status,
    ]
    return "complete" if all(str(value or "").strip() for value in required) else "incomplete"


def medtech_pure_play_flag(industry: str, subsector: str) -> int:
    labels = {normalize_subsector(industry), normalize_subsector(subsector)}
    pure_play_labels = {
        "medical_devices",
        "medical_instruments_and_supplies",
        "diagnostics_and_research",
    }
    return int(bool(labels & pure_play_labels))


def parse_universe_rows(path: Path, *, config: dict[str, Any] | None = None) -> list[UniverseCompany]:
    config = config or {}
    rows = read_csv_flexible(path)
    companies: list[UniverseCompany] = []
    seen: set[str] = set()
    active_listing_statuses = normalized_set(
        cfg_get(config, "universe_validation.active_listing_statuses", list(DEFAULT_ACTIVE_LISTING_STATUSES)),
        DEFAULT_ACTIVE_LISTING_STATUSES,
    )
    non_investable_listing_statuses = normalized_set(
        cfg_get(
            config,
            "universe_validation.non_investable_listing_statuses",
            list(DEFAULT_NON_INVESTABLE_LISTING_STATUSES),
        ),
        DEFAULT_NON_INVESTABLE_LISTING_STATUSES,
    )
    investable_security_types = normalized_set(
        cfg_get(config, "universe_validation.investable_security_types", list(DEFAULT_INVESTABLE_SECURITY_TYPES)),
        DEFAULT_INVESTABLE_SECURITY_TYPES,
    )
    for raw in rows:
        ticker = normalize_ticker(row_get(raw, "ticker", "Ticker", "Name", "MatchedTicker"))
        matched_ticker = normalize_ticker(row_get(raw, "MatchedTicker", "ticker", "Ticker", "Name"))
        if matched_ticker and ticker and matched_ticker != ticker:
            raise ValueError(f"Ticker mismatch in {path}: ticker={ticker} matched={matched_ticker}")
        if not ticker:
            continue
        if ticker in seen:
            raise ValueError(f"Duplicate ticker in {path}: {ticker}")
        seen.add(ticker)

        company_name = row_get(raw, "company_name", "Company_Name", "CompanyName")
        company_name = company_name or row_get(raw, "Name")
        sector = row_get(raw, "sector", "Sector", "Industry")
        industry = row_get(raw, "industry", "IndustryGroup", "Index")
        subsector = normalize_subsector(row_get(raw, "medtech_subsector", "Subsector", "Index"))
        security_type = row_get(raw, "security_type", "SecurityType")
        listing_status = row_get(raw, "listing_status", "ListingStatus")
        status = universe_status_from_flags(raw)
        investability_status, universe_status, is_active = derive_investability_status(
            manual_status=status,
            listing_status=listing_status,
            security_type=security_type,
            active_listing_statuses=active_listing_statuses,
            non_investable_listing_statuses=non_investable_listing_statuses,
            investable_security_types=investable_security_types,
        )
        source_aliases = tuple(
            alias
            for alias in {
                ticker,
                row_get(raw, "Company_Name"),
                row_get(raw, "CompanyName"),
                row_get(raw, "MatchedTicker"),
            }
            if str(alias or "").strip()
        )
        company = UniverseCompany(
            ticker=ticker,
            investability_status=investability_status,
            company_name=company_name,
            cik=normalize_cik(row_get(raw, "cik", "CIK")),
            exchange=row_get(raw, "exchange", "Exchange"),
            sector=sector,
            industry=industry,
            subsector=subsector,
            country=row_get(raw, "country", "Country"),
            currency=row_get(raw, "currency", "Currency"),
            security_type=security_type,
            listing_status=listing_status,
            is_primary_listing=1 if as_bool(row_get(raw, "is_primary_listing", "IsPrimaryListing")) else 0,
            universe_status=universe_status,
            is_active=is_active,
            medtech_pure_play_flag=medtech_pure_play_flag(industry, subsector),
            source_aliases=source_aliases,
            data_quality_status="",
        )
        companies.append(replace(company, data_quality_status=data_quality_status(company)))
    return companies


def source_exists(conn: Any, source_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return row is not None


def upsert_universe(conn: Any, companies: list[UniverseCompany], *, source_id: str | None = "sec_company_tickers") -> int:
    now = utc_now()
    active_source_id = source_id if source_id and source_exists(conn, source_id) else None
    if source_id and active_source_id is None:
        LOGGER.warning("Source %s not found; universe identifiers will be inserted without source_id", source_id)
    for company in companies:
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, exchange, sector, industry, subsector,
                country, currency, universe_status, is_active, medtech_pure_play_flag,
                data_quality_status, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = excluded.cik,
                company_name = excluded.company_name,
                exchange = excluded.exchange,
                sector = excluded.sector,
                industry = excluded.industry,
                subsector = excluded.subsector,
                country = excluded.country,
                currency = excluded.currency,
                universe_status = excluded.universe_status,
                is_active = excluded.is_active,
                medtech_pure_play_flag = excluded.medtech_pure_play_flag,
                data_quality_status = excluded.data_quality_status,
                updated_at = excluded.updated_at
            """,
            (
                company.ticker,
                company.cik,
                company.company_name,
                company.exchange,
                company.sector,
                company.industry,
                company.subsector,
                company.country,
                company.currency,
                company.universe_status,
                company.is_active,
                company.medtech_pure_play_flag,
                company.data_quality_status,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (company.ticker,)).fetchone()
        if row is None:
            raise RuntimeError(f"Company upsert failed for {company.ticker}")
        company_id = int(row["company_id"])
        conn.execute(
            """
            INSERT INTO dim_security(
                company_id, ticker, exchange, security_type, listing_status, is_primary_listing,
                currency, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, exchange) DO UPDATE SET
                company_id = excluded.company_id,
                security_type = excluded.security_type,
                listing_status = excluded.listing_status,
                is_primary_listing = excluded.is_primary_listing,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (
                company_id,
                company.ticker,
                company.exchange,
                company.security_type,
                company.listing_status,
                company.is_primary_listing,
                company.currency,
                now,
                now,
            ),
        )
        if company.cik:
            conn.execute(
                """
                INSERT INTO dim_identifier(
                    company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
                )
                VALUES (?, 'CIK', ?, ?, 1.0, ?, ?)
                ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                    company_id = excluded.company_id,
                    source_id = excluded.source_id,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (company_id, company.cik, active_source_id, now, now),
            )
        for alias in company.source_aliases:
            alias_norm = normalize_org_name(alias)
            if not alias_norm:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO dim_company_alias(
                    company_id, alias_raw, alias_norm, source_id, confidence, is_manual, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1.0, 0, ?, ?)
                """,
                (company_id, alias, alias_norm, active_source_id, now, now),
            )
    return len(companies)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = (
        args.universe_csv.expanduser().resolve()
        if args.universe_csv
        else resolve_path(cfg_get(config, "med_devices_universe.seed_csv"), base_dir=base_dir)
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    if not universe_csv.exists():
        raise FileNotFoundError(f"Med-devices universe CSV not found: {universe_csv}")

    companies = parse_universe_rows(universe_csv, config=config)
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="load_med_device_universe", input_path=universe_csv)
        try:
            row_count = upsert_universe(conn, companies)
            active_count = sum(1 for company in companies if company.is_active)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=row_count,
                message=f"active={active_count} source={universe_csv}",
            )
            LOGGER.info("Loaded med-devices universe: rows=%d active=%d db=%s", row_count, active_count, db_path)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
