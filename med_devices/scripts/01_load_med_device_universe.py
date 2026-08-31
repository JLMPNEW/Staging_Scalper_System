#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
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
DEFAULT_NON_INVESTABLE_EXCHANGES = {
    "otc",
    "otc_markets",
    "otc_pink",
    "otcqb",
    "otcqx",
    "pink_sheets",
}


@dataclass(frozen=True)
class UniverseCompany:
    ticker: str
    investability_status: str
    company_name: str
    cik: str
    cusip: str
    listing_start_date: str
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


@dataclass(frozen=True)
class UniverseAction:
    ticker: str
    action: str
    valid_from: date
    valid_to: date | None
    reviewed_at: date
    reason: str
    destination_pipeline: str
    source_reference: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the med-devices ticker universe into the independent DB.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--universe-actions-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
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


def normalize_cusip(raw: object) -> str:
    value = "".join(str(raw or "").strip().upper().split())
    if not value:
        return ""
    if len(value) != 9 or any(not (char.isdigit() or "A" <= char <= "Z" or char in "*@#") for char in value):
        raise ValueError(f"Invalid CUSIP: {raw!r}")

    def cusip_value(char: str) -> int:
        if char.isdigit():
            return int(char)
        if "A" <= char <= "Z":
            return ord(char) - ord("A") + 10
        return {"*": 36, "@": 37, "#": 38}[char]

    total = 0
    for idx, char in enumerate(value[:8]):
        weighted = cusip_value(char) * (2 if idx % 2 else 1)
        total += weighted // 10 + weighted % 10
    expected_check_digit = (10 - total % 10) % 10
    if not value[-1].isdigit() or int(value[-1]) != expected_check_digit:
        raise ValueError(f"Invalid CUSIP check digit: {raw!r}")
    return value


def normalize_iso_date(raw: object, *, field_name: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {raw!r}; expected YYYY-MM-DD") from exc
    return parsed.isoformat()


SUPPORTED_UNIVERSE_ACTIONS = {"exclude_all_history"}


def action_date(raw: object, *, field_name: str, required: bool = True) -> date | None:
    value = normalize_iso_date(raw, field_name=field_name)
    if not value:
        if required:
            raise ValueError(f"Missing required {field_name}")
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_universe_actions(path: Path | None) -> dict[str, UniverseAction]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Configured med-device universe action CSV does not exist: {path}")
    actions: dict[str, UniverseAction] = {}
    for row_number, row in enumerate(read_csv_flexible(path), start=2):
        ticker = normalize_ticker(row_get(row, "ticker"))
        if not ticker:
            raise ValueError(f"{path}:{row_number}: ticker is required")
        if ticker in actions:
            raise ValueError(f"{path}:{row_number}: duplicate universe action ticker {ticker}")
        action = row_get(row, "action").lower()
        if action not in SUPPORTED_UNIVERSE_ACTIONS:
            raise ValueError(
                f"{path}:{row_number}: unsupported action {action!r}; "
                f"expected one of {sorted(SUPPORTED_UNIVERSE_ACTIONS)}"
            )
        valid_from = action_date(row_get(row, "valid_from"), field_name=f"{ticker} valid_from")
        valid_to = action_date(
            row_get(row, "valid_to"),
            field_name=f"{ticker} valid_to",
            required=False,
        )
        reviewed_at = action_date(row_get(row, "reviewed_at"), field_name=f"{ticker} reviewed_at")
        if valid_from is None or reviewed_at is None:
            raise AssertionError("Required action dates unexpectedly parsed as None")
        if valid_to is not None and valid_to < valid_from:
            raise ValueError(f"{path}:{row_number}: valid_to precedes valid_from for {ticker}")
        reason = row_get(row, "reason")
        source_reference = row_get(row, "source_reference")
        if not reason or not source_reference:
            raise ValueError(f"{path}:{row_number}: reason and source_reference are required for {ticker}")
        actions[ticker] = UniverseAction(
            ticker=ticker,
            action=action,
            valid_from=valid_from,
            valid_to=valid_to,
            reviewed_at=reviewed_at,
            reason=reason,
            destination_pipeline=row_get(row, "destination_pipeline"),
            source_reference=source_reference,
        )
    return actions


def universe_action_is_effective(action: UniverseAction, *, asof: date) -> bool:
    if asof < action.valid_from:
        return False
    return action.valid_to is None or asof <= action.valid_to


def apply_universe_actions(
    companies: list[UniverseCompany],
    actions: dict[str, UniverseAction],
    *,
    asof: date,
) -> tuple[list[UniverseCompany], list[str]]:
    company_tickers = {company.ticker for company in companies}
    unmatched = sorted(set(actions) - company_tickers)
    if unmatched:
        raise ValueError(
            "Universe action ledger contains ticker(s) missing from the med-device seed: "
            + ",".join(unmatched)
        )
    applied: list[str] = []
    out: list[UniverseCompany] = []
    for company in companies:
        action = actions.get(company.ticker)
        if action is None or not universe_action_is_effective(action, asof=asof):
            out.append(company)
            continue
        if action.action == "exclude_all_history":
            out.append(
                replace(
                    company,
                    investability_status="sector_scope_excluded",
                    universe_status="remove",
                    is_active=0,
                )
            )
            applied.append(company.ticker)
            continue
        raise AssertionError(f"Unhandled universe action: {action.action}")
    return out, sorted(applied)


def validate_security_identity(
    company: UniverseCompany,
    security_identity_overrides: dict[str, Any],
) -> None:
    spec = security_identity_overrides.get(company.ticker)
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise ValueError(f"Security identity override for {company.ticker} must be a mapping")
    reviewed_at = normalize_iso_date(spec.get("reviewed_at"), field_name=f"{company.ticker} reviewed_at")
    reason = str(spec.get("reason") or "").strip()
    if not reviewed_at or not reason:
        raise ValueError(f"Security identity override for {company.ticker} requires reviewed_at and reason")

    expected = {
        "cik": normalize_cik(spec.get("cik")),
        "cusip": normalize_cusip(spec.get("cusip")),
        "listing_start_date": normalize_iso_date(
            spec.get("listing_start_date"),
            field_name=f"{company.ticker} listing_start_date",
        ),
    }
    observed = {
        "cik": company.cik,
        "cusip": company.cusip,
        "listing_start_date": company.listing_start_date,
    }
    missing_expected = [field for field, value in expected.items() if not value]
    if missing_expected:
        raise ValueError(
            f"Security identity override for {company.ticker} is incomplete: {','.join(missing_expected)}"
        )
    mismatches = [
        f"{field}: expected={expected[field]!r} observed={observed[field]!r}"
        for field in expected
        if observed[field] != expected[field]
    ]
    if mismatches:
        raise ValueError(f"Security identity mismatch for {company.ticker}: {'; '.join(mismatches)}")


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
    exchange: str,
    listing_status: str,
    security_type: str,
    active_listing_statuses: set[str],
    non_investable_exchanges: set[str],
    non_investable_listing_statuses: set[str],
    investable_security_types: set[str],
) -> tuple[str, str, int]:
    if manual_status == "remove":
        return "manual_exclude", "remove", 0
    exchange_key = normalize_subsector(exchange)
    listing_key = normalize_subsector(listing_status)
    security_key = normalize_subsector(security_type)
    if exchange_key in non_investable_exchanges:
        # Continue tracking active OTC issuers for research and event monitoring,
        # but never represent the security as exchange-investable.
        return "non_investable_exchange", "active_non_investable_otc", 1
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
    non_investable_exchanges = normalized_set(
        cfg_get(
            config,
            "universe_validation.non_investable_exchanges",
            list(DEFAULT_NON_INVESTABLE_EXCHANGES),
        ),
        DEFAULT_NON_INVESTABLE_EXCHANGES,
    )
    raw_listing_overrides = cfg_get(config, "universe_validation.ticker_listing_overrides", {})
    if not isinstance(raw_listing_overrides, dict):
        raise ValueError("universe_validation.ticker_listing_overrides must be a mapping")
    ticker_listing_overrides = {
        normalize_ticker(ticker): spec for ticker, spec in raw_listing_overrides.items()
    }
    raw_security_identity_overrides = cfg_get(config, "universe_validation.security_identity_overrides", {})
    if not isinstance(raw_security_identity_overrides, dict):
        raise ValueError("universe_validation.security_identity_overrides must be a mapping")
    security_identity_overrides = {
        normalize_ticker(ticker): spec for ticker, spec in raw_security_identity_overrides.items()
    }
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
        exchange = row_get(raw, "exchange", "Exchange")
        listing_status = row_get(raw, "listing_status", "ListingStatus")
        listing_override = ticker_listing_overrides.get(ticker)
        if listing_override is not None:
            if not isinstance(listing_override, dict):
                raise ValueError(f"Ticker listing override for {ticker} must be a mapping")
            reviewed_at = str(listing_override.get("reviewed_at") or "").strip()
            reason = str(listing_override.get("reason") or "").strip()
            if not reviewed_at or not reason:
                raise ValueError(f"Ticker listing override for {ticker} requires reviewed_at and reason")
            exchange = str(listing_override.get("exchange") or exchange).strip()
            listing_status = str(listing_override.get("listing_status") or listing_status).strip()
        status = universe_status_from_flags(raw)
        investability_status, universe_status, is_active = derive_investability_status(
            manual_status=status,
            exchange=exchange,
            listing_status=listing_status,
            security_type=security_type,
            active_listing_statuses=active_listing_statuses,
            non_investable_exchanges=non_investable_exchanges,
            non_investable_listing_statuses=non_investable_listing_statuses,
            investable_security_types=investable_security_types,
        )
        source_aliases = tuple(
            alias
            for alias in {
                ticker,
                row_get(raw, "company_name", "Company_Name"),
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
            cusip=normalize_cusip(row_get(raw, "cusip", "CUSIP")),
            listing_start_date=normalize_iso_date(
                row_get(raw, "listing_start_date", "ListingStartDate"),
                field_name=f"{ticker} listing_start_date",
            ),
            exchange=exchange,
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
        validate_security_identity(company, security_identity_overrides)
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
        if company.is_primary_listing:
            conn.execute(
                """
                UPDATE dim_security
                SET is_primary_listing = 0,
                    listing_status = CASE
                        WHEN LOWER(TRIM(COALESCE(listing_status, ''))) = 'active' THEN 'delisted'
                        ELSE listing_status
                    END,
                    updated_at = ?
                WHERE company_id = ? AND ticker = ? AND exchange <> ? AND is_primary_listing = 1
                """,
                (now, company_id, company.ticker, company.exchange),
            )
        conn.execute(
            """
            INSERT INTO dim_security(
                company_id, ticker, exchange, security_type, listing_status, is_primary_listing,
                currency, listing_start_date, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, exchange) DO UPDATE SET
                company_id = excluded.company_id,
                security_type = excluded.security_type,
                listing_status = excluded.listing_status,
                is_primary_listing = excluded.is_primary_listing,
                currency = excluded.currency,
                listing_start_date = excluded.listing_start_date,
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
                company.listing_start_date,
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
        if company.cusip:
            existing_cusip = conn.execute(
                """
                SELECT company_id
                FROM dim_identifier
                WHERE identifier_type = 'CUSIP' AND identifier_value = ?
                """,
                (company.cusip,),
            ).fetchone()
            if existing_cusip is not None and int(existing_cusip["company_id"]) != company_id:
                raise ValueError(
                    f"CUSIP {company.cusip} is already assigned to company_id={existing_cusip['company_id']}; "
                    f"refusing reassignment to {company.ticker}"
                )
            conn.execute(
                """
                INSERT INTO dim_identifier(
                    company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
                )
                VALUES (?, 'CUSIP', ?, NULL, 1.0, ?, ?)
                ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                    company_id = excluded.company_id,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (company_id, company.cusip, now, now),
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
    action_raw = str(cfg_get(config, "med_devices_universe.universe_actions_csv", "") or "").strip()
    universe_actions_csv = (
        args.universe_actions_csv.expanduser().resolve()
        if args.universe_actions_csv
        else (resolve_path(action_raw, base_dir=base_dir) if action_raw else None)
    )
    asof_text = args.asof.strip() or datetime.now(timezone.utc).date().isoformat()
    action_asof = action_date(asof_text, field_name="universe action asof")
    if action_asof is None:
        raise AssertionError("Universe action as-of unexpectedly parsed as None")
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    if not universe_csv.exists():
        raise FileNotFoundError(f"Med-devices universe CSV not found: {universe_csv}")

    companies = parse_universe_rows(universe_csv, config=config)
    universe_actions = load_universe_actions(universe_actions_csv)
    companies, applied_actions = apply_universe_actions(companies, universe_actions, asof=action_asof)
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
                message=(
                    f"active={active_count} source={universe_csv} action_asof={action_asof.isoformat()} "
                    f"applied_universe_actions={len(applied_actions)}"
                ),
            )
            LOGGER.info(
                "Loaded med-devices universe: rows=%d active=%d actions=%d db=%s",
                row_count,
                active_count,
                len(applied_actions),
                db_path,
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
