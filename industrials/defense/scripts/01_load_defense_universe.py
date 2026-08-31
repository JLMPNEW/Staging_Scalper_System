#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import load_yaml_map, read_csv_flexible, row_get  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.listing_dates import ListingWindow, bound_membership_window, listing_window_for_ticker, load_listing_windows  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import as_bool, normalize_cik, normalize_label, normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("load_defense_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "load_defense_universe"
LOAD_STAGE = "defense_universe_load"
STALE_ACTIVE_TICKER_PURGE_TABLES = (
    "fact_price_ohlcv",
    "fact_market_snapshot",
    "feature_market_technical",
    "fact_sec_filing",
    "dim_issuer_reporting_profile",
    "fact_sec_xbrl_fact_raw",
    "fact_sec_xbrl_fact",
    "fact_financial_statement_canonical",
    "feature_financial_statement",
)


@dataclass(frozen=True)
class CohortAssignment:
    cohort_id: str
    cohort_name: str
    calibration_use: str


@dataclass(frozen=True)
class DefenseCompany:
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
    model_family: str
    calibration_cohort_id: str
    calibration_cohort: str
    calibration_use: str
    development_stage: str
    universe_status: str
    is_active: int
    data_quality_status: str


@dataclass(frozen=True)
class CikOverride:
    ticker: str
    cik: str
    company_name: str
    override_type: str
    applies_to: str
    source: str
    reason: str
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the active defense universe into industrials.sqlite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--listing-dates-csv", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--cohorts", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def normalized_set(raw: Any) -> set[str]:
    values = raw if isinstance(raw, list) else []
    return {normalize_label(value) for value in values if str(value or "").strip()}


def load_cik_overrides(path: Path) -> dict[str, CikOverride]:
    if not path.exists():
        raise FileNotFoundError(
            f"CIK/ticker overrides CSV named by config key industrials_universe.cik_ticker_overrides_csv "
            f"does not exist: {path}. A missing overrides file would silently revert documented CIK "
            "corrections; fix the path or the file instead of proceeding without overrides."
        )
    overrides: dict[str, CikOverride] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        cik = normalize_cik(row_get(row, "cik"))
        if not ticker and not cik:
            continue
        if not ticker:
            raise ValueError(f"{path}: CIK override row missing ticker")
        if not cik:
            raise ValueError(f"{path}: CIK override for {ticker} missing valid cik")
        if ticker in overrides:
            raise ValueError(f"{path}: duplicate CIK override ticker={ticker}")
        overrides[ticker] = CikOverride(
            ticker=ticker,
            cik=cik,
            company_name=row_get(row, "company_name"),
            override_type=row_get(row, "override_type") or "corrected_sec_cik",
            applies_to=(row_get(row, "applies_to") or "both").lower(),
            source=row_get(row, "source"),
            reason=row_get(row, "reason"),
            notes=row_get(row, "notes"),
        )
    return overrides


def cik_for_ticker(raw_cik: object, ticker: str, overrides: dict[str, CikOverride], *, scope: str) -> str:
    override = overrides.get(ticker)
    if override is not None and override.applies_to in {scope, "both"}:
        return override.cik
    return normalize_cik(raw_cik)


def load_cohort_assignments(path: Path, *, expected_model_family: str) -> dict[str, CohortAssignment]:
    data = load_yaml_map(path)
    model_family = str(data.get("model_family") or "").strip()
    if model_family and model_family != expected_model_family:
        raise ValueError(f"{path} model_family={model_family!r} does not match expected {expected_model_family!r}")
    cohorts = data.get("cohorts")
    if not isinstance(cohorts, list):
        raise ValueError(f"{path} must contain a cohorts list.")

    out: dict[str, CohortAssignment] = {}
    for idx, raw in enumerate(cohorts, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Cohort row {idx} in {path} must be a mapping.")
        cohort_id = str(raw.get("cohort_id") or "").strip()
        cohort_name = str(raw.get("cohort_name") or "").strip()
        calibration_use = normalize_label(raw.get("calibration_use") or "core") or "core"
        tickers = raw.get("tickers")
        if not cohort_id or not cohort_name:
            raise ValueError(f"Cohort row {idx} in {path} must include cohort_id and cohort_name.")
        if not isinstance(tickers, list) or not tickers:
            raise ValueError(f"Cohort {cohort_id} in {path} must include non-empty tickers.")
        for raw_ticker in tickers:
            ticker = normalize_ticker(raw_ticker)
            if not ticker:
                continue
            if ticker in out:
                raise ValueError(f"Ticker {ticker} appears in multiple defense cohorts.")
            out[ticker] = CohortAssignment(cohort_id=cohort_id, cohort_name=cohort_name, calibration_use=calibration_use)
    return out


def source_exists(conn: Any, source_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return row is not None


def source_id_or_none(conn: Any, source_id: str) -> str | None:
    return source_id if source_exists(conn, source_id) else None


def derive_universe_status(
    *,
    investability_status: str,
    listing_status: str,
    security_type: str,
    active_listing_statuses: set[str],
    non_investable_listing_statuses: set[str],
    investable_security_types: set[str],
) -> tuple[str, int]:
    investability_key = normalize_label(investability_status)
    listing_key = normalize_label(listing_status)
    security_key = normalize_label(security_type)
    if investability_key.startswith("non_investable") or listing_key in non_investable_listing_statuses:
        return "non_investable", 0
    if investability_key in {"remove", "excluded", "manual_exclude"}:
        return "remove", 0
    if listing_key and listing_key not in active_listing_statuses:
        return "review_listing_status", 1
    if security_key and security_key not in investable_security_types:
        return "review_security_type", 1
    if investability_key and investability_key != "investable":
        return "review", 1
    return "keep", 1


def parse_universe_rows(
    path: Path,
    *,
    policy: dict[str, Any],
    cohort_map: dict[str, CohortAssignment],
    cohort_path: Path,
    model_family: str,
    config: dict[str, Any],
    cik_overrides: dict[str, CikOverride],
) -> list[DefenseCompany]:
    rows = read_csv_flexible(path)
    companies: list[DefenseCompany] = []
    seen: set[str] = set()
    expected_ticker_count = int(policy.get("expected_ticker_count") or cfg_get(config, "industrials_universe.expected_ticker_count", 0) or 0)
    required_non_cik = [str(field) for field in policy.get("required_non_cik_fields", [])]
    active_listing_statuses = normalized_set(policy.get("active_listing_statuses", ["active"])) or {"active"}
    non_investable_listing_statuses = normalized_set(policy.get("non_investable_listing_statuses", []))
    investable_security_types = normalized_set(policy.get("investable_security_types", []))
    development_stage_cohorts = {str(value).strip() for value in policy.get("development_stage_cohorts", [])}
    default_sector = str(policy.get("default_sector") or cfg_get(config, "industrials_universe.sector", "Industrials"))
    default_industry = str(policy.get("default_industry") or cfg_get(config, "industrials_universe.industry", "Aerospace & Defense"))
    default_subsector = str(policy.get("default_subsector") or cfg_get(config, "industrials_universe.subsector", "Defense"))
    default_country = str(policy.get("default_country") or cfg_get(config, "industrials_universe.country", "United States"))
    default_currency = str(policy.get("default_currency") or cfg_get(config, "industrials_universe.currency", "USD"))
    allow_unassigned = bool(policy.get("allow_unassigned_cohort", False))
    unassigned_id = str(policy.get("default_unassigned_cohort_id") or "defense_unassigned").strip()
    unassigned_name = str(policy.get("default_unassigned_cohort_name") or "Unassigned defense review").strip()
    unassigned_use = normalize_label(policy.get("default_unassigned_calibration_use") or "review") or "review"

    for raw in rows:
        ticker = normalize_ticker(row_get(raw, "ticker", "Ticker", "symbol", "Symbol"))
        if not ticker:
            continue
        if ticker in seen:
            raise ValueError(f"Duplicate ticker in {path}: {ticker}")
        seen.add(ticker)
        missing = [
            field
            for field in required_non_cik
            if field != "ticker"
            and not (allow_unassigned and field in {"defense_calibration_cohort", "calibration_cohort"})
            and not row_get(raw, field, field.title())
        ]
        if missing:
            raise ValueError(f"{ticker}: missing required CSV fields: {', '.join(missing)}")
        assignment = cohort_map.get(ticker)
        used_unassigned_assignment = False
        if assignment is None:
            if not allow_unassigned:
                raise ValueError(f"{ticker}: missing from defense cohort mapping {cohort_path}")
            assignment = CohortAssignment(
                cohort_id=unassigned_id,
                cohort_name=unassigned_name,
                calibration_use=unassigned_use,
            )
            used_unassigned_assignment = True
        csv_cohort = row_get(raw, "defense_calibration_cohort", "calibration_cohort")
        if csv_cohort and csv_cohort != assignment.cohort_id and not (allow_unassigned and used_unassigned_assignment):
            raise ValueError(f"{ticker}: CSV cohort {csv_cohort!r} does not match cohort YAML {assignment.cohort_id!r}")
        investability_status = row_get(raw, "investability_status") or "investable"
        listing_status = row_get(raw, "listing_status") or "active"
        security_type = row_get(raw, "security_type") or "Common Stock"
        universe_status, is_active = derive_universe_status(
            investability_status=investability_status,
            listing_status=listing_status,
            security_type=security_type,
            active_listing_statuses=active_listing_statuses,
            non_investable_listing_statuses=non_investable_listing_statuses,
            investable_security_types=investable_security_types,
        )
        development_stage = "development_stage" if assignment.cohort_id in development_stage_cohorts or assignment.calibration_use == "development_stage" else "operating"
        company = DefenseCompany(
            ticker=ticker,
            investability_status=investability_status,
            company_name=row_get(raw, "company_name", "company", "name"),
            cik=cik_for_ticker(row_get(raw, "cik", "CIK"), ticker, cik_overrides, scope="active"),
            exchange=row_get(raw, "exchange", "Exchange"),
            sector=row_get(raw, "sector") or default_sector,
            industry=row_get(raw, "industry") or default_industry,
            subsector=row_get(raw, "subsector") or default_subsector,
            country=row_get(raw, "country") or default_country,
            currency=row_get(raw, "currency") or default_currency,
            security_type=security_type,
            listing_status=listing_status,
            is_primary_listing=1 if as_bool(row_get(raw, "is_primary_listing"), default=True) else 0,
            model_family=model_family,
            calibration_cohort_id=assignment.cohort_id,
            calibration_cohort=assignment.cohort_name,
            calibration_use=assignment.calibration_use,
            development_stage=development_stage,
            universe_status=universe_status,
            is_active=is_active,
            data_quality_status="",
        )
        required_values = [
            company.ticker,
            company.company_name,
            company.exchange,
            company.sector,
            company.industry,
            company.subsector,
            company.country,
            company.currency,
            company.security_type,
            company.listing_status,
            company.calibration_cohort_id,
        ]
        data_quality_status = "complete" if all(str(value or "").strip() for value in required_values) and company.cik else "incomplete_identity"
        companies.append(DefenseCompany(**{**company.__dict__, "data_quality_status": data_quality_status}))

    if expected_ticker_count > 0 and len(companies) != expected_ticker_count:
        raise ValueError(f"{path} must contain exactly {expected_ticker_count} unique defense tickers; found {len(companies)}")
    if set(cohort_map).difference({company.ticker for company in companies}):
        missing = sorted(set(cohort_map).difference({company.ticker for company in companies}))
        raise ValueError(f"Cohort YAML contains tickers missing from active defense CSV: {missing}")
    return companies


def add_issue(
    conn: Any,
    *,
    ticker: str,
    company_id: int | None,
    issue_type: str,
    issue_detail: str,
    severity: str = "warning",
    source_id: str | None = None,
    model_family: str,
) -> None:
    # SC-12: issues are family-scoped; stamp model_family so per-stage clears for
    # one family never wipe another family's open issues.
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, LOAD_STAGE, model_family, ticker, company_id, source_id, issue_type, issue_detail, now, now),
    )


def insert_alias(conn: Any, *, company_id: int, alias_raw: str, source_id: str | None, confidence: float = 1.0) -> None:
    alias_raw = str(alias_raw or "").strip()
    if not alias_raw:
        return
    alias_norm = normalize_org_name(alias_raw)
    if not alias_norm:
        return
    now = utc_now()
    exists = conn.execute(
        """
        SELECT 1 FROM dim_company_alias
        WHERE company_id = ? AND alias_raw = ? AND alias_norm = ? LIMIT 1
        """,
        (company_id, alias_raw, alias_norm),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO dim_company_alias(
            company_id, alias_raw, alias_norm, source_id, confidence, is_manual, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (company_id, alias_raw, alias_norm, source_id, confidence, now, now),
    )


def upsert_current_membership(
    conn: Any,
    *,
    company: DefenseCompany,
    company_id: int,
    source_id: str,
    start_date: str,
    end_date: str | None,
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
        VALUES (?, ?, ?, ?, 'current_source_of_truth', ?, ?, ?, ?, 1, ?, ?, ?, ?)
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
            company.ticker,
            company.model_family,
            source_id,
            start_date,
            end_date,
            "active" if company.is_active else "inactive",
            int(company.is_active),
            confidence,
            reason,
            now,
            now,
        ),
    )


def membership_window_for_company(
    *,
    company: DefenseCompany,
    listing_windows: dict[str, ListingWindow],
    default_start_date: str,
) -> tuple[str, str | None, float, str]:
    window = listing_window_for_ticker(listing_windows, company.ticker)
    start_date, end_date, listing_confidence, listing_reason = bound_membership_window(
        default_start_date=default_start_date,
        default_end_date=None,
        listing_window=window,
    )
    if end_date and end_date < start_date:
        raise ValueError(f"{company.ticker}: listing-date window has end_date {end_date} before start_date {start_date}")
    reason = (
        "Seeded from active defense_tickers.csv and bounded by the defense listing-date contract "
        "for historical PIT replays. "
        f"{listing_reason}"
    )
    confidence = min(1.0, max(0.0, listing_confidence if window is not None else 1.0))
    return start_date, end_date, confidence, reason


def upsert_universe(
    conn: Any,
    companies: list[DefenseCompany],
    *,
    source_id: str,
    cohort_source_id: str,
    start_date: str,
    listing_windows: dict[str, ListingWindow],
    preserved_historical_tickers: set[str],
) -> int:
    now = utc_now()
    seed_source_id = source_id_or_none(conn, source_id)
    if seed_source_id is None:
        raise ValueError(f"Source registry is missing required source_id={source_id}")
    cohort_source_id_or_none = source_id_or_none(conn, cohort_source_id)
    sec_source_id = source_id_or_none(conn, "sec_company_tickers")
    tickers = [company.ticker for company in companies]
    placeholders = ",".join("?" for _ in tickers)
    # SC-12: family-scoped clear so this load never wipes another family's open
    # issues for the same ticker/stage.
    conn.execute(
        f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({placeholders})",
        (LOAD_STAGE, companies[0].model_family, *tickers),
    )
    stale_count = reset_stale_active_seed_entities(
        conn,
        model_family=companies[0].model_family,
        seed_source_id=seed_source_id,
        incoming_tickers=set(tickers),
        preserved_historical_tickers=preserved_historical_tickers,
    )
    if stale_count:
        LOGGER.info("Removed stale active defense seed entities: count=%d", stale_count)
    conn.execute(
        """
        DELETE FROM dim_universe_membership
        WHERE model_family = ?
          AND membership_source_id = ?
        """,
        (companies[0].model_family, seed_source_id),
    )

    for company in companies:
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, sector, industry, subsector, country, currency,
                universe_status, is_active, data_quality_status, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = excluded.cik,
                company_name = excluded.company_name,
                sector = excluded.sector,
                industry = excluded.industry,
                subsector = excluded.subsector,
                country = excluded.country,
                currency = excluded.currency,
                universe_status = excluded.universe_status,
                is_active = excluded.is_active,
                data_quality_status = excluded.data_quality_status,
                updated_at = excluded.updated_at
            """,
            (
                company.ticker,
                company.cik,
                company.company_name,
                company.sector,
                company.industry,
                company.subsector,
                company.country,
                company.currency,
                company.universe_status,
                company.is_active,
                company.data_quality_status,
                now,
                now,
            ),
        )
        company_id_row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (company.ticker,)).fetchone()
        if company_id_row is None:
            raise RuntimeError(f"Company upsert failed for {company.ticker}")
        company_id = int(company_id_row["company_id"])

        conn.execute(
            "DELETE FROM dim_security WHERE ticker = ? AND exchange = 'historical_delisted'",
            (company.ticker,),
        )
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
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
            )
            VALUES (?, 'TICKER', ?, ?, 1.0, ?, ?)
            ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
                source_id = COALESCE(excluded.source_id, dim_identifier.source_id),
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (company_id, company.ticker, seed_source_id, now, now),
        )
        if company.cik:
            conn.execute(
                """
                DELETE FROM dim_identifier
                WHERE company_id = ?
                  AND identifier_type = 'CIK'
                  AND identifier_value <> ?
                """,
                (company_id, company.cik),
            )
            conn.execute(
                """
                INSERT INTO dim_identifier(
                    company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
                )
                VALUES (?, 'CIK', ?, ?, 1.0, ?, ?)
                ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
                    source_id = COALESCE(excluded.source_id, dim_identifier.source_id),
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (company_id, company.cik, sec_source_id, now, now),
            )
        insert_alias(conn, company_id=company_id, alias_raw=company.ticker, source_id=seed_source_id)
        insert_alias(conn, company_id=company_id, alias_raw=company.company_name, source_id=seed_source_id)
        conn.execute(
            """
            INSERT INTO dim_industrials_taxonomy(
                company_id, ticker, model_family, sector, industry, subsector,
                calibration_cohort_id, calibration_cohort, calibration_use, development_stage,
                taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, 0, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                company_id = excluded.company_id,
                sector = excluded.sector,
                industry = excluded.industry,
                subsector = excluded.subsector,
                calibration_cohort_id = excluded.calibration_cohort_id,
                calibration_cohort = excluded.calibration_cohort,
                calibration_use = excluded.calibration_use,
                development_stage = excluded.development_stage,
                taxonomy_confidence = excluded.taxonomy_confidence,
                taxonomy_source = excluded.taxonomy_source,
                updated_at = excluded.updated_at
            """,
            (
                company_id,
                company.ticker,
                company.model_family,
                company.sector,
                company.industry,
                company.subsector,
                company.calibration_cohort_id,
                company.calibration_cohort,
                company.calibration_use,
                company.development_stage,
                cohort_source_id,
                now,
            ),
        )
        membership_start_date, membership_end_date, membership_confidence, membership_reason = membership_window_for_company(
            company=company,
            listing_windows=listing_windows,
            default_start_date=start_date,
        )
        upsert_current_membership(
            conn,
            company=company,
            company_id=company_id,
            source_id=seed_source_id,
            start_date=membership_start_date,
            end_date=membership_end_date,
            confidence=membership_confidence,
            reason=membership_reason,
        )

        if company.data_quality_status != "complete":
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type="incomplete_identity",
                issue_detail="Active defense CSV row is missing CIK or another required identity field.",
                severity="error",
                model_family=company.model_family,
            )
        if company.universe_status != "keep":
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type="not_rank_ready_universe_status",
                issue_detail=f"Universe status is {company.universe_status}.",
                severity="warning",
                model_family=company.model_family,
            )
        if company.is_primary_listing != 1:
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type="non_primary_share_class",
                issue_detail=(
                    "Ticker is a non-primary listing/share class. Downstream scoring and ranking "
                    "must deduplicate or explicitly allow it before rank-ready use."
                ),
                severity="warning",
                model_family=company.model_family,
            )
        if cohort_source_id_or_none is None:
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=None,
                issue_type="missing_cohort_source_registry",
                issue_detail=f"Source registry is missing cohort source id {cohort_source_id}.",
                severity="warning",
                model_family=company.model_family,
            )
    return len(companies)


def reset_stale_active_seed_entities(
    conn: Any,
    *,
    model_family: str,
    seed_source_id: str,
    incoming_tickers: set[str],
    preserved_historical_tickers: set[str],
) -> int:
    existing_rows = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM dim_universe_membership
        WHERE model_family = ?
          AND membership_source_id = ?
          AND membership_basis = 'current_source_of_truth'
        """,
        (model_family, seed_source_id),
    ).fetchall()
    stale_ticker_set = {str(row["ticker"]) for row in existing_rows} - incoming_tickers
    # Derive predecessor transitions from the governed historical contract, not
    # only from current-seed rows that happen to remain in the database. A prior
    # partial load may already have deleted the old current membership while
    # leaving dim_company.is_active=1; relying on stale_ticker_set then leaks the
    # predecessor back into current publishers (ISSC after the IA transition).
    transition_candidates = sorted(preserved_historical_tickers.difference(incoming_tickers))
    transitioned_tickers: list[str] = []
    if transition_candidates:
        candidate_placeholders = ",".join("?" for _ in transition_candidates)
        rows = conn.execute(
            f"""
            SELECT c.ticker
            FROM dim_company c
            WHERE c.ticker IN ({candidate_placeholders})
              AND c.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM dim_universe_membership m
                  WHERE m.company_id = c.company_id
                    AND m.is_current_member = 1
                    AND NOT (
                        m.model_family = ?
                        AND m.membership_source_id = ?
                        AND m.membership_basis = 'current_source_of_truth'
                    )
              )
            ORDER BY c.ticker
            """,
            (*transition_candidates, model_family, seed_source_id),
        ).fetchall()
        transitioned_tickers = [str(row["ticker"]) for row in rows]
    stale_tickers = sorted(stale_ticker_set.difference(preserved_historical_tickers))
    if transitioned_tickers:
        LOGGER.info(
            "Preserving predecessor data for explicit historical transition(s): %s",
            ",".join(transitioned_tickers),
        )
        transitioned_placeholders = ",".join("?" for _ in transitioned_tickers)
        conn.execute(
            f"""
            UPDATE dim_company
            SET universe_status = 'historical_transition',
                is_active = 0,
                updated_at = ?
            WHERE ticker IN ({transitioned_placeholders})
              AND is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM dim_universe_membership m
                  WHERE m.company_id = dim_company.company_id
                    AND m.is_current_member = 1
                    AND NOT (
                        m.model_family = ?
                        AND m.membership_source_id = ?
                        AND m.membership_basis = 'current_source_of_truth'
                    )
              )
            """,
            (utc_now(), *transitioned_tickers, model_family, seed_source_id),
        )
    if not stale_tickers:
        return 0

    placeholders = ",".join("?" for _ in stale_tickers)
    # SC-12: scope the stale-ticker issue purge to this model family; another
    # family may still track the ticker and owns its open issues.
    conn.execute(
        f"DELETE FROM data_quality_issues WHERE model_family = ? AND ticker IN ({placeholders})",
        (model_family, *stale_tickers),
    )
    purge_ticker_scoped_rows(conn, tickers=stale_tickers)
    deleted = conn.execute(
        f"""
        DELETE FROM dim_company
        WHERE ticker IN ({placeholders})
          AND is_active = 1
          AND NOT EXISTS (
              SELECT 1
              FROM dim_universe_membership m
              WHERE m.company_id = dim_company.company_id
                AND NOT (m.model_family = ? AND m.membership_source_id = ?)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dim_delisted_calibration_seed d
              WHERE d.model_family = ?
                AND (d.ticker = dim_company.ticker OR d.internal_ticker = dim_company.ticker)
          )
        """,
        (*stale_tickers, model_family, seed_source_id, model_family),
    ).rowcount
    return int(deleted if deleted is not None else 0)


def purge_ticker_scoped_rows(conn: Any, *, tickers: list[str]) -> None:
    placeholders = ",".join("?" for _ in tickers)
    params = tuple(tickers)
    for table in STALE_ACTIVE_TICKER_PURGE_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE ticker IN ({placeholders})", params)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = args.universe_csv.expanduser().resolve() if args.universe_csv else resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    listing_dates_csv = args.listing_dates_csv.expanduser().resolve() if args.listing_dates_csv else resolve_path(cfg_get(config, "industrials_universe.listing_dates_csv"), base_dir=base_dir)
    historical_membership_csv = resolve_path(
        cfg_get(config, "industrials_universe.historical_membership_csv"),
        base_dir=base_dir,
    )
    delisted_seed_csv = resolve_path(
        cfg_get(config, "industrials_universe.delisted_seed_csv"),
        base_dir=base_dir,
    )
    policy_path = args.policy.expanduser().resolve() if args.policy else resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir)
    cohort_path = args.cohorts.expanduser().resolve() if args.cohorts else resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    cik_overrides_path = resolve_path(cfg_get(config, "industrials_universe.cik_ticker_overrides_csv"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense")).strip()
    source_id = str(cfg_get(config, "industrials_universe.seed_source_id", "defense_ticker_seed"))
    cohort_source_id = str(cfg_get(config, "industrials_universe.cohort_source_id", "defense_cohort_policy"))
    start_date = str(cfg_get(config, "industrials_universe.optimization_start_date", "2010-01-01"))
    policy = load_yaml_map(policy_path)
    cohort_map = load_cohort_assignments(cohort_path, expected_model_family=model_family)
    cik_overrides = load_cik_overrides(cik_overrides_path)
    listing_windows = load_listing_windows(listing_dates_csv)
    preserved_historical_tickers = {
        ticker
        for path in (historical_membership_csv, delisted_seed_csv)
        for row in read_csv_flexible(path)
        if (ticker := normalize_ticker(row_get(row, "ticker")))
    }
    companies = parse_universe_rows(
        universe_csv,
        policy=policy,
        cohort_map=cohort_map,
        cohort_path=cohort_path,
        model_family=model_family,
        config=config,
        cik_overrides=cik_overrides,
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=universe_csv)
        try:
            with conn:
                row_count = upsert_universe(
                    conn,
                    companies,
                    source_id=source_id,
                    cohort_source_id=cohort_source_id,
                    start_date=start_date,
                    listing_windows=listing_windows,
                    preserved_historical_tickers=preserved_historical_tickers,
                )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=row_count,
                message=f"model_family={model_family} rows={row_count}",
            )
            LOGGER.info("Loaded active defense universe: db=%s rows=%d", db_path, row_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
