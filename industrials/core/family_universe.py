from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from industrials.core.config import load_yaml
from industrials.core.csv_utils import read_csv_flexible
from industrials.core.db import utc_now
from industrials.core.text_norm import as_bool, normalize_cik, normalize_org_name, normalize_ticker


ACTIVE_REQUIRED_COLUMNS = {
    "ticker",
    "investability_status",
    "company_name",
    "cik",
    "exchange",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "security_type",
    "listing_status",
    "is_primary_listing",
    "calibration_cohort",
}
DELISTED_REQUIRED_COLUMNS = {
    "ticker",
    "company",
    "cohort",
    "exit_type",
    "terminal_type",
    "acquirer",
    "exit_year",
    "cik",
    "confidence",
}
HISTORICAL_REQUIRED_COLUMNS = {
    "internal_ticker",
    "exchange_ticker",
    "price_source_symbol",
    "company_name",
    "cik",
    "exchange",
    "country",
    "currency",
    "security_type",
    "calibration_cohort_id",
    "calibration_cohort",
    "start_date",
    "end_date",
    "membership_status",
    "successor_ticker",
    "event_type",
    "confidence",
    "source_url",
    "notes",
}
ALIAS_REQUIRED_COLUMNS = {
    "contract_ticker",
    "active_ticker",
    "predecessor_ticker",
    "effective_date",
    "price_history_csv",
    "issuer_id",
    "reason",
    "source",
    "verified_flag",
    "notes",
}


def csv_headers(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return {str(value or "").strip() for value in (csv.DictReader(handle).fieldnames or [])}
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode CSV header: {path}")


def require_headers(path: Path, required: set[str]) -> None:
    missing = sorted(required - csv_headers(path))
    if missing:
        raise ValueError(f"{path}: missing required columns={missing}")


def parse_date(raw: object, *, field: str, ticker: str, allow_blank: bool = False) -> str:
    text = str(raw or "").strip()[:10]
    if allow_blank and not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid {field}={raw!r}; expected YYYY-MM-DD") from exc


def confidence_score(raw: object) -> float:
    text = str(raw or "").strip().lower()
    labels = {"verified": 0.95, "high": 0.90, "medium": 0.70, "low": 0.50}
    if text in labels:
        return labels[text]
    try:
        return min(1.0, max(0.0, float(text)))
    except ValueError:
        return 0.60


def load_cohorts(path: Path, *, model_family: str) -> dict[str, dict[str, str]]:
    payload = load_yaml(path)
    if str(payload.get("model_family") or "").strip() != model_family:
        raise ValueError(f"{path}: model_family must be {model_family}")
    raw_cohorts = payload.get("cohorts")
    if not isinstance(raw_cohorts, list) or not raw_cohorts:
        raise ValueError(f"{path}: cohorts must be a non-empty list")
    cohorts: dict[str, dict[str, str]] = {}
    for raw in raw_cohorts:
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: invalid cohort row={raw!r}")
        cohort_id = str(raw.get("cohort_id") or "").strip()
        cohort_name = str(raw.get("cohort_name") or "").strip()
        if not cohort_id or not cohort_name:
            raise ValueError(f"{path}: every cohort needs cohort_id and cohort_name")
        if cohort_id in cohorts:
            raise ValueError(f"{path}: duplicate cohort_id={cohort_id}")
        cohorts[cohort_id] = {
            "cohort_name": cohort_name,
            "calibration_use": str(raw.get("calibration_use") or "core").strip(),
            "development_stage": str(raw.get("development_stage") or "operating").strip(),
        }
    return cohorts


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def validate_seed_contracts(
    *,
    active_path: Path,
    delisted_path: Path,
    cohort_path: Path,
    policy_path: Path,
    model_family: str,
) -> tuple[list[str], list[str], dict[str, int]]:
    require_headers(active_path, ACTIVE_REQUIRED_COLUMNS)
    require_headers(delisted_path, DELISTED_REQUIRED_COLUMNS)
    active = read_csv_flexible(active_path)
    delisted = read_csv_flexible(delisted_path)
    cohorts = load_cohorts(cohort_path, model_family=model_family)
    policy = load_yaml(policy_path)
    errors: list[str] = []
    warnings: list[str] = []
    expected_active = int(policy.get("expected_ticker_count") or 0)
    expected_delisted = int(policy.get("expected_delisted_count") or 0)
    if expected_active and len(active) != expected_active:
        errors.append(f"active count expected={expected_active} actual={len(active)}")
    if expected_delisted and len(delisted) != expected_delisted:
        errors.append(f"delisted count expected={expected_delisted} actual={len(delisted)}")
    active_tickers = [normalize_ticker(row.get("ticker")) for row in active]
    delisted_tickers = [normalize_ticker(row.get("ticker")) for row in delisted]
    if "" in active_tickers:
        errors.append("active seed contains a blank/invalid ticker")
    if "" in delisted_tickers:
        errors.append("delisted seed contains a blank/invalid ticker")
    if duplicate := _duplicates(active_tickers):
        errors.append(f"duplicate active tickers={duplicate}")
    if duplicate := _duplicates(delisted_tickers):
        errors.append(f"duplicate delisted tickers={duplicate}")
    if overlap := sorted(set(active_tickers).intersection(delisted_tickers)):
        errors.append(f"active/delisted ticker overlap requires internalization={overlap}")
    allowed_security_types = {
        str(value).strip().lower() for value in policy.get("investable_security_types", [])
    }
    allowed_listing_statuses = {
        str(value).strip().lower() for value in policy.get("active_listing_statuses", [])
    }
    required_columns = [str(value) for value in policy.get("required_columns", ACTIVE_REQUIRED_COLUMNS)]
    for row in active:
        ticker = normalize_ticker(row.get("ticker")) or "<blank>"
        missing = [column for column in required_columns if not str(row.get(column) or "").strip()]
        if missing:
            errors.append(f"{ticker}: missing active fields={missing}")
        cohort = str(row.get("calibration_cohort") or "").strip()
        if cohort not in cohorts:
            errors.append(f"{ticker}: unknown calibration_cohort={cohort!r}")
        if str(row.get("investability_status") or "").strip().lower() != "investable":
            errors.append(f"{ticker}: active seed must be investable")
        security_type = str(row.get("security_type") or "").strip().lower()
        if allowed_security_types and security_type not in allowed_security_types:
            errors.append(f"{ticker}: unsupported security_type={security_type!r}")
        status = str(row.get("listing_status") or "").strip().lower()
        if allowed_listing_statuses and status not in allowed_listing_statuses:
            errors.append(f"{ticker}: unsupported listing_status={status!r}")
        if normalize_cik(row.get("cik")) == "":
            errors.append(f"{ticker}: invalid/blank CIK")
    represented_delisted: set[str] = set()
    valid_terminal = {str(v).strip() for v in policy.get("allowed_terminal_types", [])}
    for row in delisted:
        ticker = normalize_ticker(row.get("ticker")) or "<blank>"
        missing = [column for column in DELISTED_REQUIRED_COLUMNS if not str(row.get(column) or "").strip()]
        if missing:
            errors.append(f"{ticker}: missing delisted fields={missing}")
        cohort = str(row.get("cohort") or "").strip()
        represented_delisted.add(cohort)
        if cohort not in cohorts:
            errors.append(f"{ticker}: unknown delisted cohort={cohort!r}")
        terminal = str(row.get("terminal_type") or "").strip()
        if valid_terminal and terminal not in valid_terminal:
            errors.append(f"{ticker}: invalid terminal_type={terminal!r}")
        year = str(row.get("exit_year") or "").strip()
        if not year.isdigit() or not 1900 <= int(year) <= 2100:
            errors.append(f"{ticker}: invalid exit_year={year!r}")
    for cohort_id in sorted(set(cohorts) - represented_delisted):
        warnings.append(f"no curated delisted rows for cohort={cohort_id}")
    counts = {"active": len(active), "delisted": len(delisted), "cohorts": len(cohorts)}
    return errors, warnings, counts


def load_listing_dates(path: Path) -> dict[str, dict[str, str]]:
    required = {
        "ticker",
        "first_eligible_date",
        "last_eligible_date",
        "eligibility_basis",
        "source",
        "confidence",
        "notes",
    }
    require_headers(path, required)
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker or ticker in out:
            raise ValueError(f"{path}: blank or duplicate listing ticker={ticker!r}")
        first = parse_date(row.get("first_eligible_date"), field="first_eligible_date", ticker=ticker)
        last = parse_date(row.get("last_eligible_date"), field="last_eligible_date", ticker=ticker, allow_blank=True)
        if last and last < first:
            raise ValueError(f"{ticker}: last_eligible_date precedes first_eligible_date")
        row["first_eligible_date"] = first
        row["last_eligible_date"] = last
        out[ticker] = row
    return out


def _company_id(conn: sqlite3.Connection, ticker: str) -> int:
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        raise RuntimeError(f"Company upsert failed for {ticker}")
    return int(row["company_id"])


def _upsert_company(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str,
    company_name: str,
    sector: str,
    industry: str,
    subsector: str,
    country: str,
    currency: str,
    is_active: bool,
    universe_status: str,
    quality_status: str,
) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, sector, industry, subsector, country, currency,
            universe_status, is_active, data_quality_status, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = COALESCE(NULLIF(excluded.cik, ''), dim_company.cik),
            company_name = COALESCE(NULLIF(excluded.company_name, ''), dim_company.company_name),
            country = COALESCE(NULLIF(excluded.country, ''), dim_company.country),
            currency = COALESCE(NULLIF(excluded.currency, ''), dim_company.currency),
            is_active = MAX(dim_company.is_active, excluded.is_active),
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            cik,
            company_name,
            sector,
            industry,
            subsector,
            country,
            currency,
            universe_status,
            int(is_active),
            quality_status,
            now,
            now,
        ),
    )
    return _company_id(conn, ticker)


def _upsert_security(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    exchange: str,
    security_type: str,
    listing_status: str,
    is_primary: bool,
    currency: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_security(
            company_id, ticker, exchange, security_type, listing_status,
            is_primary_listing, currency, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, exchange) DO UPDATE SET
            company_id=excluded.company_id, security_type=excluded.security_type,
            listing_status=excluded.listing_status,
            is_primary_listing=excluded.is_primary_listing,
            currency=excluded.currency, updated_at=excluded.updated_at
        """,
        (
            company_id,
            ticker,
            exchange,
            security_type,
            listing_status,
            int(is_primary),
            currency,
            now,
            now,
        ),
    )


def _upsert_identifier(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    identifier_type: str,
    value: str,
    source_id: str,
    confidence: float = 1.0,
) -> None:
    if not value:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_identifier(
            company_id, identifier_type, identifier_value, source_id,
            confidence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
            source_id=excluded.source_id, confidence=excluded.confidence,
            updated_at=excluded.updated_at
        """,
        (company_id, identifier_type, value, source_id, confidence, now, now),
    )


def _upsert_taxonomy(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    model_family: str,
    sector: str,
    industry: str,
    subsector: str,
    cohort_id: str,
    cohort: dict[str, str],
    source_id: str,
    historical: bool = False,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_industrials_taxonomy(
            company_id, ticker, model_family, sector, industry, subsector,
            calibration_cohort_id, calibration_cohort, calibration_use,
            development_stage, taxonomy_confidence, taxonomy_source,
            analyst_reviewed, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, 1, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            company_id=excluded.company_id, sector=excluded.sector,
            industry=excluded.industry, subsector=excluded.subsector,
            calibration_cohort_id=excluded.calibration_cohort_id,
            calibration_cohort=excluded.calibration_cohort,
            calibration_use=excluded.calibration_use,
            development_stage=excluded.development_stage,
            taxonomy_source=excluded.taxonomy_source,
            analyst_reviewed=excluded.analyst_reviewed,
            updated_at=excluded.updated_at
        """,
        (
            company_id,
            ticker,
            model_family,
            sector,
            industry,
            subsector,
            cohort_id,
            cohort["cohort_name"],
            "historical_research" if historical else cohort["calibration_use"],
            "historical_delisted" if historical else cohort["development_stage"],
            source_id,
            now,
        ),
    )


def _upsert_membership(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    model_family: str,
    source_id: str,
    basis: str,
    start_date: str,
    end_date: str,
    status: str,
    current: bool,
    confidence: float,
    reason: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id,
            membership_basis, start_date, end_date, membership_status,
            is_current_member, point_in_time_flag, confidence, reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, membership_source_id, start_date) DO UPDATE SET
            company_id=excluded.company_id, membership_basis=excluded.membership_basis,
            end_date=excluded.end_date, membership_status=excluded.membership_status,
            is_current_member=excluded.is_current_member,
            point_in_time_flag=excluded.point_in_time_flag,
            confidence=excluded.confidence, reason=excluded.reason,
            updated_at=excluded.updated_at
        """,
        (
            company_id,
            ticker,
            model_family,
            source_id,
            basis,
            start_date,
            end_date,
            status,
            int(current),
            confidence,
            reason[:500],
            now,
            now,
        ),
    )


def load_active_universe(
    conn: sqlite3.Connection,
    *,
    active_path: Path,
    delisted_path: Path,
    listing_path: Path,
    cohort_path: Path,
    policy_path: Path,
    model_family: str,
    seed_source_id: str,
    cohort_source_id: str,
) -> int:
    errors, _warnings, counts = validate_seed_contracts(
        active_path=active_path,
        delisted_path=delisted_path,
        cohort_path=cohort_path,
        policy_path=policy_path,
        model_family=model_family,
    )
    if errors:
        raise ValueError("; ".join(errors[:25]))
    active = read_csv_flexible(active_path)
    cohorts = load_cohorts(cohort_path, model_family=model_family)
    listings = load_listing_dates(listing_path)
    tickers = {normalize_ticker(row["ticker"]) for row in active}
    if missing := sorted(tickers - set(listings)):
        raise ValueError(f"Active tickers missing listing-date contracts={missing[:25]}")
    conn.execute(
        "DELETE FROM dim_universe_membership WHERE model_family=? AND membership_source_id=?",
        (model_family, seed_source_id),
    )
    conn.execute("DELETE FROM dim_identifier WHERE source_id=?", (seed_source_id,))
    for row in active:
        ticker = normalize_ticker(row["ticker"])
        company_id = _upsert_company(
            conn,
            ticker=ticker,
            cik=normalize_cik(row["cik"]),
            company_name=row["company_name"],
            sector=row["sector"],
            industry=row["industry"],
            subsector=row["subsector"],
            country=row["country"],
            currency=row["currency"],
            is_active=True,
            universe_status="investable",
            quality_status="seed_validated",
        )
        _upsert_security(
            conn,
            company_id=company_id,
            ticker=ticker,
            exchange=row["exchange"],
            security_type=row["security_type"],
            listing_status=row["listing_status"],
            is_primary=as_bool(row["is_primary_listing"]),
            currency=row["currency"],
        )
        _upsert_identifier(
            conn,
            company_id=company_id,
            identifier_type="CIK",
            value=normalize_cik(row["cik"]),
            source_id=seed_source_id,
        )
        cohort_id = row["calibration_cohort"]
        _upsert_taxonomy(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            sector=row["sector"],
            industry=row["industry"],
            subsector=row["subsector"],
            cohort_id=cohort_id,
            cohort=cohorts[cohort_id],
            source_id=cohort_source_id,
        )
        listing = listings[ticker]
        _upsert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            source_id=seed_source_id,
            basis="current_source_of_truth",
            start_date=listing["first_eligible_date"],
            end_date=listing["last_eligible_date"],
            status="active",
            current=not bool(listing["last_eligible_date"]),
            confidence=confidence_score(listing["confidence"]),
            reason=(
                f"Active transportation seed bounded by listing contract; "
                f"basis={listing['eligibility_basis']}; source={listing['source']}"
            ),
        )
    stale = conn.execute(
        "SELECT ticker FROM dim_industrials_taxonomy WHERE model_family=?",
        (model_family,),
    ).fetchall()
    stale_tickers = {normalize_ticker(row["ticker"]) for row in stale} - tickers
    for ticker in stale_tickers:
        has_history = conn.execute(
            "SELECT 1 FROM dim_universe_membership WHERE model_family=? AND ticker=? LIMIT 1",
            (model_family, ticker),
        ).fetchone()
        if has_history is None:
            conn.execute(
                "DELETE FROM dim_industrials_taxonomy WHERE model_family=? AND ticker=?",
                (model_family, ticker),
            )
    if counts["active"] != len(tickers):
        raise RuntimeError("Active seed count changed during normalization")
    return len(tickers)


def _internal_delisted_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    exit_year: int,
    model_family: str,
) -> str:
    collision = conn.execute(
        """
        SELECT 1 FROM dim_universe_membership
        WHERE ticker=? AND (is_current_member=1 OR model_family<>?) LIMIT 1
        """,
        (ticker, model_family),
    ).fetchone()
    if collision is None:
        return ticker
    candidate = f"{ticker}-DEL{exit_year}"
    normalized = normalize_ticker(candidate)
    if not normalized:
        raise ValueError(f"Cannot internalize reused delisted ticker={ticker}")
    return normalized


def load_historical_and_delisted(
    conn: sqlite3.Connection,
    *,
    historical_path: Path,
    delisted_path: Path,
    cohort_path: Path,
    model_family: str,
    historical_source_id: str,
    delisted_source_id: str,
    default_start_date: str,
) -> tuple[int, int]:
    require_headers(historical_path, HISTORICAL_REQUIRED_COLUMNS)
    require_headers(delisted_path, DELISTED_REQUIRED_COLUMNS)
    cohorts = load_cohorts(cohort_path, model_family=model_family)
    historical = read_csv_flexible(historical_path)
    delisted = read_csv_flexible(delisted_path)
    historical_industry_by_ticker = {
        normalize_ticker(row["ticker"]): str(row.get("industry") or "").strip()
        for row in delisted
        if normalize_ticker(row["ticker"]) and str(row.get("industry") or "").strip()
    }
    resolved_delisted_history = {
        normalize_ticker(row["exchange_ticker"]): row
        for row in historical
        if normalize_ticker(row["exchange_ticker"]) and str(row.get("end_date") or "").strip()
    }
    conn.execute(
        "DELETE FROM dim_universe_membership WHERE model_family=? AND membership_source_id IN (?, ?)",
        (model_family, historical_source_id, delisted_source_id),
    )
    conn.execute("DELETE FROM dim_delisted_calibration_seed WHERE model_family=?", (model_family,))
    for row in historical:
        ticker = normalize_ticker(row["internal_ticker"])
        cohort_id = row["calibration_cohort_id"]
        if not ticker or cohort_id not in cohorts:
            raise ValueError(f"Invalid historical row ticker={ticker!r} cohort={cohort_id!r}")
        start = parse_date(row["start_date"], field="start_date", ticker=ticker)
        end = parse_date(row["end_date"], field="end_date", ticker=ticker, allow_blank=True)
        # The historical membership contract intentionally carries lifecycle
        # fields, not a second copy of the active taxonomy. Preserve the
        # detailed industry already loaded from the active source of truth,
        # and use an optional reviewed industry on the delisted seed for
        # historical-only issuers. Otherwise this second-stage loader would
        # replace Railroads, Trucking, Airlines, and Marine Shipping with the
        # generic family label and silently break metric applicability.
        existing_taxonomy = conn.execute(
            """
            SELECT sector, industry, subsector
            FROM dim_industrials_taxonomy
            WHERE ticker=? AND model_family=?
            """,
            (ticker, model_family),
        ).fetchone()
        if not end and existing_taxonomy is not None:
            sector = str(existing_taxonomy["sector"] or "Industrials")
            industry = str(existing_taxonomy["industry"] or "Transportation")
            subsector = str(existing_taxonomy["subsector"] or "Transportation")
        else:
            sector = "Industrials"
            industry = historical_industry_by_ticker.get(
                normalize_ticker(row.get("exchange_ticker")),
                "Transportation",
            )
            subsector = "Transportation"
        company_id = _upsert_company(
            conn,
            ticker=ticker,
            cik=normalize_cik(row["cik"]),
            company_name=row["company_name"],
            sector=sector,
            industry=industry,
            subsector=subsector,
            country=row["country"],
            currency=row["currency"],
            is_active=not bool(end),
            universe_status="investable" if not end else "historical_delisted",
            quality_status="historical_membership_validated",
        )
        _upsert_taxonomy(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            sector=sector,
            industry=industry,
            subsector=subsector,
            cohort_id=cohort_id,
            cohort=cohorts[cohort_id],
            source_id=historical_source_id,
            historical=bool(end),
        )
        _upsert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            source_id=historical_source_id,
            basis="survivorship_corrected_pit_contract",
            start_date=start,
            end_date=end,
            status=row["membership_status"],
            current=not bool(end),
            confidence=confidence_score(row["confidence"]),
            reason=row["notes"],
        )
    for row in delisted:
        ticker = normalize_ticker(row["ticker"])
        exit_year = int(row["exit_year"])
        cohort_id = row["cohort"]
        if not ticker or cohort_id not in cohorts:
            raise ValueError(f"Invalid delisted row ticker={ticker!r} cohort={cohort_id!r}")
        resolved = resolved_delisted_history.get(ticker)
        internal = (
            normalize_ticker(resolved["internal_ticker"])
            if resolved
            else _internal_delisted_ticker(
                conn, ticker=ticker, exit_year=exit_year, model_family=model_family
            )
        )
        confidence = confidence_score(row["confidence"])
        historical_industry = str(row.get("industry") or "Transportation").strip()
        company_id = _upsert_company(
            conn,
            ticker=internal,
            cik=normalize_cik(row["cik"]),
            company_name=row["company"],
            sector="Industrials",
            industry=historical_industry,
            subsector="Transportation",
            country="",
            currency="USD",
            is_active=False,
            universe_status="historical_delisted",
            quality_status="delisted_seed_validated",
        )
        _upsert_security(
            conn,
            company_id=company_id,
            ticker=internal,
            exchange="historical_delisted",
            security_type="Common Stock",
            listing_status="historical_delisted",
            is_primary=False,
            currency="USD",
        )
        _upsert_identifier(
            conn,
            company_id=company_id,
            identifier_type="CIK",
            value=normalize_cik(row["cik"]),
            source_id=delisted_source_id,
            confidence=confidence,
        )
        _upsert_taxonomy(
            conn,
            company_id=company_id,
            ticker=internal,
            model_family=model_family,
            sector="Industrials",
            industry=historical_industry,
            subsector="Transportation",
            cohort_id=cohort_id,
            cohort=cohorts[cohort_id],
            source_id=delisted_source_id,
            historical=True,
        )
        start_date = (
            parse_date(resolved["start_date"], field="start_date", ticker=internal)
            if resolved
            else default_start_date
        )
        end_date = (
            parse_date(resolved["end_date"], field="end_date", ticker=internal)
            if resolved
            else f"{exit_year:04d}-12-31"
        )
        boundary_note = (
            "Norgate-resolved final quoted date"
            if resolved
            else "provisional year-end boundary; excluded from calibration until price reconciliation"
        )
        _upsert_membership(
            conn,
            company_id=company_id,
            ticker=internal,
            model_family=model_family,
            source_id=delisted_source_id,
            basis="delisted_calibration_seed",
            start_date=start_date,
            end_date=end_date,
            status="delisted",
            current=False,
            confidence=confidence,
            reason=(
                f"{row['exit_type']}; terminal_type={row['terminal_type']}; "
                f"acquirer={row['acquirer']}; {boundary_note}"
            ),
        )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_delisted_calibration_seed(
                ticker, internal_ticker, model_family, company_name,
                calibration_cohort_id, exit_type, terminal_type, acquirer,
                exit_year, cik, confidence_label, confidence_score, source_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                internal_ticker=excluded.internal_ticker,
                company_name=excluded.company_name,
                calibration_cohort_id=excluded.calibration_cohort_id,
                exit_type=excluded.exit_type, terminal_type=excluded.terminal_type,
                acquirer=excluded.acquirer, exit_year=excluded.exit_year,
                cik=excluded.cik, confidence_label=excluded.confidence_label,
                confidence_score=excluded.confidence_score,
                source_id=excluded.source_id, updated_at=excluded.updated_at
            """,
            (
                ticker,
                internal,
                model_family,
                row["company"],
                cohort_id,
                row["exit_type"],
                row["terminal_type"],
                row["acquirer"],
                exit_year,
                normalize_cik(row["cik"]),
                row["confidence"].lower(),
                confidence,
                delisted_source_id,
                now,
                now,
            ),
        )
    return len(historical), len(delisted)


def load_aliases(
    conn: sqlite3.Connection,
    *,
    path: Path,
    source_id: str,
) -> int:
    require_headers(path, ALIAS_REQUIRED_COLUMNS)
    rows = read_csv_flexible(path)
    conn.execute("DELETE FROM dim_ticker_alias WHERE source_id=?", (source_id,))
    conn.execute(
        "DELETE FROM fact_corporate_action WHERE source_id=? AND action_type='ticker_alias'",
        (source_id,),
    )
    seen: set[tuple[str, str]] = set()
    for row in rows:
        contract = normalize_ticker(row["contract_ticker"])
        active = normalize_ticker(row["active_ticker"])
        effective = parse_date(row["effective_date"], field="effective_date", ticker=contract)
        if not contract or not active or not as_bool(row["verified_flag"]):
            raise ValueError(f"Alias must be verified with valid tickers: {row}")
        key = (contract, effective)
        if key in seen:
            raise ValueError(f"Duplicate alias key={key}")
        seen.add(key)
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_ticker_alias(
                contract_ticker, active_ticker, predecessor_ticker,
                effective_date, price_history_csv, issuer_id, reason, source,
                verified_flag, notes, source_id, created_at, updated_at
            ) VALUES (?, ?, NULLIF(?, ''), ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(contract_ticker, effective_date) DO UPDATE SET
                active_ticker=excluded.active_ticker,
                predecessor_ticker=excluded.predecessor_ticker,
                price_history_csv=excluded.price_history_csv,
                issuer_id=excluded.issuer_id, reason=excluded.reason,
                source=excluded.source, verified_flag=excluded.verified_flag,
                notes=excluded.notes, source_id=excluded.source_id,
                updated_at=excluded.updated_at
            """,
            (
                contract,
                active,
                normalize_ticker(row["predecessor_ticker"]),
                effective,
                row["price_history_csv"],
                row["issuer_id"],
                row["reason"],
                row["source"],
                row["notes"],
                source_id,
                now,
                now,
            ),
        )
    return len(rows)


def validate_database_contract(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    active_source_id: str,
    historical_source_id: str,
    delisted_source_id: str,
    expected_active: int,
    expected_historical: int,
    expected_delisted: int,
) -> list[str]:
    checks = {
        "active_taxonomy": (
            "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family=? AND calibration_use<>'historical_research'",
            (model_family,),
            expected_active,
        ),
        "active_membership": (
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family=? AND membership_source_id=?",
            (model_family, active_source_id),
            expected_active,
        ),
        "historical_membership": (
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family=? AND membership_source_id=?",
            (model_family, historical_source_id),
            expected_historical,
        ),
        "delisted_membership": (
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family=? AND membership_source_id=?",
            (model_family, delisted_source_id),
            expected_delisted,
        ),
        "delisted_seed": (
            "SELECT COUNT(*) FROM dim_delisted_calibration_seed WHERE model_family=? AND source_id=?",
            (model_family, delisted_source_id),
            expected_delisted,
        ),
    }
    errors: list[str] = []
    for label, (sql, params, expected) in checks.items():
        actual = int(conn.execute(sql, params).fetchone()[0] or 0)
        if actual != expected:
            errors.append(f"{label}: expected={expected} actual={actual}")
    orphan = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership m
            WHERE m.model_family=? AND NOT EXISTS (
                SELECT 1 FROM dim_industrials_taxonomy t
                WHERE t.model_family=m.model_family AND t.ticker=m.ticker
            )
            """,
            (model_family,),
        ).fetchone()[0]
        or 0
    )
    if orphan:
        errors.append(f"membership rows without family taxonomy={orphan}")
    invalid = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE model_family=? AND (
                start_date='' OR (end_date IS NOT NULL AND end_date<start_date)
                OR (is_current_member=1 AND end_date IS NOT NULL)
                OR (is_current_member=0 AND end_date IS NULL)
            )
            """,
            (model_family,),
        ).fetchone()[0]
        or 0
    )
    if invalid:
        errors.append(f"invalid membership intervals/current flags={invalid}")
    return errors


def validate_identity_contract(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    active_path: Path,
    delisted_path: Path,
) -> list[str]:
    active = read_csv_flexible(active_path)
    delisted = read_csv_flexible(delisted_path)
    errors: list[str] = []
    active_tickers = {normalize_ticker(row["ticker"]) for row in active}
    db_active = {
        normalize_ticker(row["ticker"])
        for row in conn.execute(
            """
            SELECT ticker FROM dim_universe_membership
            WHERE model_family=? AND membership_basis='current_source_of_truth'
            """,
            (model_family,),
        ).fetchall()
    }
    if active_tickers != db_active:
        errors.append(
            f"active seed/DB mismatch missing={sorted(active_tickers-db_active)[:20]} "
            f"unexpected={sorted(db_active-active_tickers)[:20]}"
        )
    db_delisted = {
        normalize_ticker(row["ticker"])
        for row in conn.execute(
            "SELECT ticker FROM dim_delisted_calibration_seed WHERE model_family=?",
            (model_family,),
        ).fetchall()
    }
    expected_delisted = {normalize_ticker(row["ticker"]) for row in delisted}
    if expected_delisted != db_delisted:
        errors.append(
            f"delisted seed/DB mismatch missing={sorted(expected_delisted-db_delisted)[:20]} "
            f"unexpected={sorted(db_delisted-expected_delisted)[:20]}"
        )
    by_cik: dict[str, list[dict[str, str]]] = {}
    for row in active:
        by_cik.setdefault(normalize_cik(row["cik"]), []).append(row)
    for cik, rows in by_cik.items():
        if len(rows) <= 1:
            continue
        names = {normalize_org_name(row["company_name"]) for row in rows}
        primary = sum(as_bool(row["is_primary_listing"]) for row in rows)
        if len(names) > 1:
            errors.append(f"shared CIK {cik} has conflicting issuer names")
        if primary != 1:
            errors.append(f"shared CIK {cik} must have exactly one primary listing; actual={primary}")
    return errors
