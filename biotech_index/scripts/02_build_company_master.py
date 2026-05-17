#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.text_norm import AliasCandidate, build_company_aliases, normalize_cik, normalize_org_name, normalize_ticker


LOGGER = logging.getLogger("build_company_master")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


@dataclass(frozen=True)
class ScreenCompany:
    ticker: str
    cik: str
    company_name: str
    exchange: str
    sector: str
    industry: str
    industry_aggregate: str
    security_type: str
    is_primary_listing: str
    listing_status: str
    country: str
    currency: str
    manual_include: str
    manual_exclude: str
    manual_review: str
    notes: str
    decision: str
    reason_codes: str
    match_type: str
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build persistent biotech company master and aliases from screen output.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", type=str, default="", help="Optional YYYY-MM-DD history as-of date. Defaults to UTC today.")
    parser.add_argument("--screen-all", type=Path, default=None, help="Override screen results CSV.")
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite database path.")
    parser.add_argument("--alias-overrides", type=Path, default=None, help="Optional manual alias overrides CSV.")
    parser.add_argument("--status-overrides", type=Path, default=None, help="Optional company status overrides CSV.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid --asof date: {raw}") from exc


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(k): str(v or "") for k, v in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        raw = lowered.get(key.lower())
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def parse_screen_rows(path: Path, *, store_decisions: set[str]) -> list[ScreenCompany]:
    rows = read_csv_flexible(path)
    companies: list[ScreenCompany] = []
    for row in rows:
        decision = row_get(row, "decision").lower()
        if store_decisions and decision not in store_decisions:
            continue
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "Tickers"))
        cik = normalize_cik(row_get(row, "cik", "CIK"))
        company_name = row_get(row, "company_name", "CompanyName", "company")
        if not ticker or not company_name:
            continue
        companies.append(
            ScreenCompany(
                ticker=ticker,
                cik=cik,
                company_name=company_name,
                exchange=row_get(row, "exchange", "Exchange"),
                sector=row_get(row, "sector", "Sector"),
                industry=row_get(row, "industry", "Industry"),
                industry_aggregate=row_get(row, "industry_aggregate", "IndustryAggregate", "industryAggregate"),
                security_type=row_get(row, "security_type", "SecurityType", "quoteType", "QuoteType"),
                is_primary_listing=row_get(row, "is_primary_listing", "IsPrimaryListing", "PrimaryListing", "primary_listing"),
                listing_status=row_get(row, "listing_status", "ListingStatus", "Status", "status"),
                country=row_get(row, "country", "Country"),
                currency=row_get(row, "currency", "Currency"),
                manual_include=row_get(row, "manual_include", "ManualInclude"),
                manual_exclude=row_get(row, "manual_exclude", "ManualExclude"),
                manual_review=row_get(row, "manual_review", "ManualReview"),
                notes=row_get(row, "notes", "Notes"),
                decision=decision or "unknown",
                reason_codes=row_get(row, "reason_codes", "ReasonCodes"),
                match_type=row_get(row, "match_type", "MatchType"),
                source=row_get(row, "source", "Source"),
            )
        )
    return companies


def load_manual_aliases(path: Optional[Path]) -> dict[str, list[AliasCandidate]]:
    if path is None or not path.exists():
        return {}
    aliases: dict[str, list[AliasCandidate]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "Tickers"))
        alias_raw = row_get(row, "alias", "Alias", "alias_raw", "AliasRaw")
        if not ticker or not alias_raw:
            continue
        try:
            confidence = float(row_get(row, "confidence", "Confidence") or "1.0")
        except ValueError:
            confidence = 1.0
        aliases.setdefault(ticker, []).append(
            AliasCandidate(
                alias_raw=alias_raw,
                alias_norm=normalize_org_name(alias_raw),
                source=row_get(row, "source", "Source") or "manual_override",
                confidence=confidence,
                is_manual=True,
            )
        )
    return aliases


def load_status_overrides(path: Optional[Path]) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "Tickers"))
        if not ticker:
            continue
        overrides[ticker] = {
            "decision": row_get(row, "decision", "Decision").lower(),
            "listing_status": row_get(row, "listing_status", "ListingStatus", "status"),
            "manual_include": row_get(row, "manual_include", "ManualInclude"),
            "manual_exclude": row_get(row, "manual_exclude", "ManualExclude"),
            "manual_review": row_get(row, "manual_review", "ManualReview"),
            "reason_codes": row_get(row, "reason_codes", "ReasonCodes"),
            "notes": row_get(row, "notes", "Notes"),
        }
    return overrides


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def override_protects_absent_ticker(override: dict[str, str], *, active_decisions: set[str]) -> bool:
    if as_bool(override.get("manual_exclude")):
        return False
    decision = str(override.get("decision") or "").strip().lower()
    return decision in active_decisions or as_bool(override.get("manual_include")) or as_bool(override.get("manual_review"))


def apply_status_override(company: ScreenCompany, overrides: dict[str, dict[str, str]]) -> ScreenCompany:
    override = overrides.get(company.ticker)
    if not override:
        return company
    return replace(
        company,
        decision=override.get("decision") or company.decision,
        listing_status=override.get("listing_status") or company.listing_status,
        manual_include=override.get("manual_include") or company.manual_include,
        manual_exclude=override.get("manual_exclude") or company.manual_exclude,
        manual_review=override.get("manual_review") or company.manual_review,
        reason_codes=override.get("reason_codes") or company.reason_codes,
        notes=override.get("notes") or company.notes,
    )


def dedupe_aliases(aliases: Iterable[AliasCandidate]) -> list[AliasCandidate]:
    out: list[AliasCandidate] = []
    seen: set[tuple[str, str]] = set()
    for alias in aliases:
        if not alias.alias_norm:
            continue
        key = (alias.alias_norm, alias.source)
        if key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return out


def upsert_company(conn: Any, company: ScreenCompany, *, active_decisions: set[str]) -> int:
    now = utc_now()
    is_active = 1 if company.decision in active_decisions else 0
    row = conn.execute(
        """
        INSERT INTO companies(
            ticker, cik, company_name, exchange, sector, industry, industry_aggregate,
            security_type, is_primary_listing, listing_status, country, currency,
            manual_include, manual_exclude, manual_review, notes,
            universe_status, is_active, source_screen_decision, reason_codes,
            first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = excluded.cik,
            company_name = excluded.company_name,
            exchange = excluded.exchange,
            sector = excluded.sector,
            industry = excluded.industry,
            industry_aggregate = excluded.industry_aggregate,
            security_type = excluded.security_type,
            is_primary_listing = excluded.is_primary_listing,
            listing_status = excluded.listing_status,
            country = excluded.country,
            currency = excluded.currency,
            manual_include = excluded.manual_include,
            manual_exclude = excluded.manual_exclude,
            manual_review = excluded.manual_review,
            notes = excluded.notes,
            universe_status = excluded.universe_status,
            is_active = excluded.is_active,
            source_screen_decision = excluded.source_screen_decision,
            reason_codes = excluded.reason_codes,
            updated_at = excluded.updated_at
        RETURNING company_id
        """,
        (
            company.ticker,
            company.cik,
            company.company_name,
            company.exchange,
            company.sector,
            company.industry,
            company.industry_aggregate,
            company.security_type,
            company.is_primary_listing,
            company.listing_status,
            company.country,
            company.currency,
            company.manual_include,
            company.manual_exclude,
            company.manual_review,
            company.notes,
            company.decision,
            is_active,
            company.decision,
            company.reason_codes,
            now,
            now,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Company upsert failed for {company.ticker}")
    return int(row["company_id"])


def insert_alias(conn: Any, *, company_id: int, alias: AliasCandidate) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO company_aliases(
            company_id, alias_raw, alias_norm, source, confidence, is_manual, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            alias.alias_raw,
            alias.alias_norm,
            alias.source,
            float(alias.confidence),
            1 if alias.is_manual else 0,
            now,
            now,
        ),
    )


def insert_history(conn: Any, *, company_id: int, company: ScreenCompany, source_file: Path, run_id: int, asof_date: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO company_universe_history(
            asof_date, company_id, ticker, universe_status, reason_codes, source_file, run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (asof_date, company_id, company.ticker, company.decision, company.reason_codes, str(source_file), run_id, now),
    )


def deactivate_absent_companies(
    conn: Any,
    *,
    present_tickers: set[str],
    protected_tickers: set[str],
    source_file: Path,
    run_id: int,
) -> int:
    if not present_tickers:
        return 0
    retained_tickers = {str(ticker or "").upper() for ticker in (set(present_tickers) | set(protected_tickers))}
    active_rows = conn.execute(
        """
        SELECT company_id, ticker
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """,
    ).fetchall()
    rows = [row for row in active_rows if str(row["ticker"] or "").upper() not in retained_tickers]
    if not rows:
        return 0

    now = utc_now()
    asof_date = datetime.now(timezone.utc).date().isoformat()
    reason_codes = "absent_from_latest_screen"
    update_params = [
        ("remove", "absent_from_latest_screen", reason_codes, now, int(row["company_id"]))
        for row in rows
    ]
    history_params = [
        (
            asof_date,
            int(row["company_id"]),
            str(row["ticker"] or "").upper(),
            "remove",
            reason_codes,
            str(source_file),
            run_id,
            now,
        )
        for row in rows
    ]
    conn.executemany(
        """
        UPDATE companies
        SET is_active = 0,
            universe_status = ?,
            source_screen_decision = ?,
            reason_codes = ?,
            updated_at = ?
        WHERE company_id = ?
        """,
        update_params,
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO company_universe_history(
            asof_date, company_id, ticker, universe_status, reason_codes, source_file, run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        history_params,
    )
    return len(rows)


def build_aliases(company: ScreenCompany, manual_aliases: dict[str, list[AliasCandidate]]) -> list[AliasCandidate]:
    aliases = [
        AliasCandidate(alias_raw=company.ticker, alias_norm=normalize_org_name(company.ticker), source="ticker", confidence=1.0),
    ]
    aliases.extend(build_company_aliases(company.company_name))
    aliases.extend(manual_aliases.get(company.ticker, []))
    return dedupe_aliases(aliases)


def main() -> None:
    configure_logging()
    args = parse_args()

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent

    screen_path = args.screen_all.expanduser().resolve() if args.screen_all else resolve_path(cfg_get(config, "paths.screen_results_csv"), base_dir=base_dir)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    alias_path = args.alias_overrides.expanduser().resolve() if args.alias_overrides else resolve_optional_path(cfg_get(config, "paths.manual_alias_overrides_csv"), base_dir=base_dir)
    status_path = args.status_overrides.expanduser().resolve() if args.status_overrides else resolve_optional_path(cfg_get(config, "paths.company_status_overrides_csv"), base_dir=base_dir)
    active_decisions = {x.lower() for x in normalize_string_list(cfg_get(config, "company_master.active_decisions"), ["keep", "review"])}
    store_decisions = {x.lower() for x in normalize_string_list(cfg_get(config, "company_master.store_decisions"), ["keep", "review", "remove"])}
    deactivate_absent = str(cfg_get(config, "company_master.deactivate_absent_from_screen", True)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    history_asof_date = parse_date(args.asof)

    if not screen_path.exists():
        raise FileNotFoundError(f"Screen results CSV not found: {screen_path}")

    companies = parse_screen_rows(screen_path, store_decisions=store_decisions)
    manual_aliases = load_manual_aliases(alias_path)
    status_overrides = load_status_overrides(status_path)
    LOGGER.info("Loaded %d screen companies from %s", len(companies), screen_path)
    if alias_path and alias_path.exists():
        LOGGER.info("Loaded manual aliases from %s", alias_path)
    if status_path and status_path.exists():
        LOGGER.info("Loaded %d company status override(s) from %s", len(status_overrides), status_path)
    protected_override_tickers = {
        ticker
        for ticker, override in status_overrides.items()
        if override_protects_absent_ticker(override, active_decisions=active_decisions)
    }
    parsed_tickers = {company.ticker for company in companies}
    protected_absent_tickers = protected_override_tickers - parsed_tickers
    if protected_absent_tickers:
        LOGGER.warning(
            "Protecting %d active override ticker(s) absent from latest screen: %s",
            len(protected_absent_tickers),
            ",".join(sorted(protected_absent_tickers)[:25]),
        )

    run_id: int | None = None
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_company_master", input_path=screen_path)
        try:
            active_count = 0
            alias_count = 0
            alias_delete_ids: list[tuple[int]] = []
            alias_rows: list[tuple[Any, ...]] = []
            alias_now = utc_now()
            with conn:
                for company in companies:
                    company = apply_status_override(company, status_overrides)
                    company_id = upsert_company(conn, company, active_decisions=active_decisions)
                    alias_delete_ids.append((company_id,))
                    if company.decision in active_decisions:
                        active_count += 1
                    for alias in build_aliases(company, manual_aliases):
                        alias_rows.append(
                            (
                                company_id,
                                alias.alias_raw,
                                alias.alias_norm,
                                alias.source,
                                float(alias.confidence),
                                1 if alias.is_manual else 0,
                                alias_now,
                                alias_now,
                            )
                        )
                        alias_count += 1
                    insert_history(
                        conn,
                        company_id=company_id,
                        company=company,
                        source_file=screen_path,
                        run_id=run_id,
                        asof_date=history_asof_date,
                    )
                if alias_delete_ids:
                    conn.executemany(
                        "DELETE FROM company_aliases WHERE company_id = ? AND COALESCE(is_manual, 0) = 0",
                        alias_delete_ids,
                    )
                if alias_rows:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO company_aliases(
                            company_id, alias_raw, alias_norm, source, confidence, is_manual, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        alias_rows,
                    )
                absent_count = (
                    deactivate_absent_companies(
                        conn,
                        present_tickers=parsed_tickers,
                        protected_tickers=protected_absent_tickers,
                        source_file=screen_path,
                        run_id=run_id,
                    )
                    if deactivate_absent
                    else 0
                )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(companies),
                message=f"active={active_count} aliases={alias_count} deactivated_absent={absent_count}",
            )
            LOGGER.info("Wrote company master DB: %s", db_path)
            LOGGER.info("Companies=%d Active=%d Aliases=%d DeactivatedAbsent=%d", len(companies), active_count, alias_count, absent_count)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
