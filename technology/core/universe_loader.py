#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.db import connect, finish_run, init_db, start_run, utc_now
from technology.core.logging_utils import configure_utc_logging
from technology.core.source_registry import load_source_registry, upsert_source_registry
from technology.core.text_norm import as_bool, normalize_cik, normalize_label, normalize_org_name, normalize_ticker


LOGGER = logging.getLogger("technology_universe_loader")


@dataclass(frozen=True)
class UniverseLoadSettings:
    description: str
    default_config: Path
    default_model_family: str
    seed_source_id: str
    cohort_source_id: str
    load_stage: str = "technology_universe_load"
    default_unassigned_cohort_id: str = "unassigned"
    default_unassigned_cohort_name: str = "Unassigned review"
    cohort_label: str = "cohorts"
    source_of_truth_label: str = "universe source of truth"
    missing_cik_issue_detail: str = (
        "CIK is missing from the universe CSV; ticker is excluded from SEC-fundamental coverage until resolved."
    )
    unassigned_issue_type: str = "unassigned_calibration_cohort"
    unassigned_issue_detail: str = "Ticker is not assigned to a core calibration cohort."


@dataclass(frozen=True)
class CohortAssignment:
    cohort_id: str
    cohort_name: str
    calibration_use: str


@dataclass(frozen=True)
class UniverseCompany:
    ticker: str
    investability_status: str
    company_name: str
    cik: str
    exchange: str
    sector: str
    industry: str
    subindustry_role: str
    country: str
    currency: str
    security_type: str
    listing_status: str
    is_primary_listing: int
    model_family: str
    taxonomy_subsector: str
    calibration_cohort_id: str
    calibration_cohort: str
    calibration_use: str
    universe_status: str
    is_active: int
    data_quality_status: str


def parse_args(settings: UniverseLoadSettings, argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=settings.description)
    parser.add_argument("--config", type=Path, default=settings.default_config)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--cohorts", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args(argv)


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


def resolve_optional_path(raw: Any, *, base_dir: Path) -> Path:
    return resolve_path(raw, base_dir=base_dir)


def load_yaml_map(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load Stage 2 policy YAML.") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_cohort_assignments(
    path: Path,
    *,
    expected_model_family: str,
    cohort_label: str = "cohorts",
) -> dict[str, CohortAssignment]:
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
                raise ValueError(f"Ticker {ticker} appears in multiple {cohort_label}.")
            out[ticker] = CohortAssignment(
                cohort_id=cohort_id,
                cohort_name=cohort_name,
                calibration_use=calibration_use,
            )
    return out


def source_exists(conn: Any, source_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return row is not None


def source_id_or_none(conn: Any, source_id: str) -> str | None:
    return source_id if source_exists(conn, source_id) else None


def data_quality_status(company: UniverseCompany, *, unassigned_cohort_id: str) -> str:
    required = [
        company.ticker,
        company.company_name,
        company.exchange,
        company.sector,
        company.industry,
        company.subindustry_role,
        company.country,
        company.currency,
        company.security_type,
        company.listing_status,
    ]
    if not all(str(value or "").strip() for value in required):
        return "incomplete_required_identity"
    if not company.cik:
        return "missing_cik"
    if company.calibration_cohort_id == unassigned_cohort_id:
        return "taxonomy_review"
    return "complete"


def universe_status(company: UniverseCompany, *, active_statuses: set[str], investable_types: set[str]) -> str:
    if company.investability_status.startswith("non_investable_"):
        return company.investability_status
    if normalize_label(company.listing_status) not in active_statuses:
        return "review"
    if normalize_label(company.security_type) not in investable_types:
        return "review"
    if company.calibration_use != "core":
        return "review"
    return "keep"


def parse_universe_rows(
    path: Path,
    *,
    policy: dict[str, Any],
    cohort_map: dict[str, CohortAssignment],
    model_family: str,
    settings: UniverseLoadSettings,
) -> list[UniverseCompany]:
    rows = read_csv_flexible(path)
    companies: list[UniverseCompany] = []
    seen: set[str] = set()
    active_statuses = {normalize_label(x) for x in policy.get("active_listing_statuses", ["active"])}
    non_investable_statuses = {
        normalize_label(x)
        for x in policy.get(
            "non_investable_listing_statuses",
            ["active_financial_status_d", "active_financial_status_e", "inactive_or_not_investable", "invalid_or_inactive"],
        )
    }
    investable_types = {normalize_label(x) for x in policy.get("investable_security_types", [])}
    unassigned_id = str(policy.get("default_unassigned_cohort_id") or settings.default_unassigned_cohort_id).strip()
    unassigned_name = str(policy.get("default_unassigned_cohort_name") or settings.default_unassigned_cohort_name).strip()
    unassigned_use = normalize_label(policy.get("default_unassigned_calibration_use") or "review") or "review"
    expected_ticker_count = int(policy.get("expected_ticker_count") or 0)

    for raw in rows:
        ticker = normalize_ticker(row_get(raw, "ticker", "Ticker", "symbol", "Symbol"))
        if not ticker:
            continue
        if ticker in seen:
            raise ValueError(f"Duplicate ticker in {path}: {ticker}")
        seen.add(ticker)

        assignment = cohort_map.get(ticker)
        if assignment is None:
            assignment = CohortAssignment(
                cohort_id=unassigned_id,
                cohort_name=unassigned_name,
                calibration_use=unassigned_use,
            )
        listing_status = row_get(raw, "listing_status", "ListingStatus")
        security_type = row_get(raw, "security_type", "SecurityType")
        listing_key = normalize_label(listing_status)
        security_key = normalize_label(security_type)
        if listing_key in non_investable_statuses:
            investability_status = "non_investable_listing_status"
        elif security_key and security_key not in investable_types:
            investability_status = "non_investable_security_type"
        elif listing_key and listing_key not in active_statuses:
            investability_status = "review_listing_status"
        else:
            investability_status = "investable"
        company = UniverseCompany(
            ticker=ticker,
            investability_status=investability_status,
            company_name=row_get(raw, "company_name", "CompanyName", "company", "name"),
            cik=normalize_cik(row_get(raw, "cik", "CIK")),
            exchange=row_get(raw, "exchange", "Exchange"),
            sector=row_get(raw, "sector", "Sector") or "Technology",
            industry=row_get(raw, "industry", "Industry") or str(policy.get("default_industry") or "Technology"),
            subindustry_role=row_get(raw, "subsector", "Subsector", "subindustry_role"),
            country=row_get(raw, "country", "Country"),
            currency=row_get(raw, "currency", "Currency"),
            security_type=security_type,
            listing_status=listing_status,
            is_primary_listing=1 if as_bool(row_get(raw, "is_primary_listing", "IsPrimaryListing")) else 0,
            model_family=model_family,
            taxonomy_subsector=model_family,
            calibration_cohort_id=assignment.cohort_id,
            calibration_cohort=assignment.cohort_name,
            calibration_use=assignment.calibration_use,
            universe_status="",
            is_active=1,
            data_quality_status="",
        )
        status = universe_status(company, active_statuses=active_statuses, investable_types=investable_types)
        company = UniverseCompany(
            **{
                **company.__dict__,
                "universe_status": status,
                "is_active": 0 if status == "remove" or status.startswith("non_investable_") else 1,
            }
        )
        company = UniverseCompany(**{**company.__dict__, "data_quality_status": data_quality_status(company, unassigned_cohort_id=unassigned_id)})
        companies.append(company)
    if not companies:
        raise ValueError(f"No usable universe rows found in {path}")
    if expected_ticker_count > 0 and len(companies) != expected_ticker_count:
        raise ValueError(
            f"{path} is the {settings.source_of_truth_label} and must contain exactly "
            f"{expected_ticker_count} unique tickers; found {len(companies)}."
        )
    return companies


def clear_stage_issues(conn: Any, tickers: list[str], *, load_stage: str) -> None:
    if not tickers:
        return
    placeholders = ",".join("?" for _ in tickers)
    conn.execute(
        f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})",
        [load_stage, *tickers],
    )


def add_issue(
    conn: Any,
    *,
    ticker: str,
    company_id: int | None,
    issue_type: str,
    issue_detail: str,
    load_stage: str,
    severity: str = "warning",
    source_id: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, load_stage, ticker, company_id, source_id, issue_type, issue_detail, now, now),
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
    company: UniverseCompany,
    company_id: int,
    source_id: str | None,
) -> None:
    """Seed the current source-of-truth membership interval.

    This row is intentionally marked point_in_time_flag=0. It preserves the
    exact current production universe while giving research stages a durable
    table that can later accept true historical/delisted membership intervals.
    """
    now = utc_now()
    start_date = "2010-01-01"
    basis = "current_source_of_truth"
    if source_id is None:
        conn.execute(
            """
            DELETE FROM dim_universe_membership
            WHERE ticker = ?
              AND model_family = ?
              AND membership_source_id IS NULL
              AND membership_basis = ?
              AND start_date = ?
            """,
            (company.ticker, company.model_family, basis, start_date),
        )
        conn.execute(
            """
            DELETE FROM dim_universe_membership
            WHERE ticker = ?
              AND model_family = ?
              AND membership_source_id IS NULL
              AND membership_basis = ?
              AND start_date <> ?
            """,
            (company.ticker, company.model_family, basis, start_date),
        )
    else:
        conn.execute(
            """
            DELETE FROM dim_universe_membership
            WHERE ticker = ?
              AND model_family = ?
              AND membership_source_id = ?
              AND membership_basis = ?
              AND start_date <> ?
            """,
            (company.ticker, company.model_family, source_id, basis, start_date),
        )
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id, membership_basis,
            start_date, end_date, membership_status, is_current_member,
            point_in_time_flag, confidence, reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?)
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
            basis,
            start_date,
            "active" if int(company.is_active) == 1 else "inactive",
            int(company.is_active),
            0.75,
            "Seeded from current source-of-truth universe; not a historical point-in-time backfill.",
            now,
            now,
        ),
    )


def upsert_universe(
    conn: Any,
    companies: list[UniverseCompany],
    *,
    settings: UniverseLoadSettings,
    unassigned_cohort_id: str,
) -> int:
    now = utc_now()
    seed_source_id = source_id_or_none(conn, settings.seed_source_id)
    cohort_source_id = source_id_or_none(conn, settings.cohort_source_id)
    sec_source_id = source_id_or_none(conn, "sec_company_tickers")
    clear_stage_issues(conn, [company.ticker for company in companies], load_stage=settings.load_stage)

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
                company.subindustry_role,
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
            INSERT INTO dim_technology_taxonomy(
                company_id, ticker, model_family, sector, subsector, calibration_cohort_id,
                calibration_cohort, subindustry_role, calibration_use, liquidity_instrument_flag,
                taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                company_id = excluded.company_id,
                sector = excluded.sector,
                subsector = excluded.subsector,
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
                company.ticker,
                company.model_family,
                company.sector,
                company.taxonomy_subsector,
                company.calibration_cohort_id,
                company.calibration_cohort,
                company.subindustry_role,
                company.calibration_use,
                "primary_listing" if company.is_primary_listing else "non_primary_or_secondary_listing",
                1.0 if company.calibration_use == "core" else 0.5,
                settings.cohort_source_id,
                now,
            ),
        )

        upsert_current_membership(
            conn,
            company=company,
            company_id=company_id,
            source_id=seed_source_id,
        )

        missing_required = [
            field
            for field, value in {
                "company_name": company.company_name,
                "exchange": company.exchange,
                "sector": company.sector,
                "industry": company.industry,
                "subsector": company.subindustry_role,
                "country": company.country,
                "currency": company.currency,
                "security_type": company.security_type,
                "listing_status": company.listing_status,
            }.items()
            if not str(value or "").strip()
        ]
        for field in missing_required:
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type="missing_required_identity_field",
                issue_detail=f"Missing required field: {field}",
                load_stage=settings.load_stage,
                severity="error",
            )
        if not company.cik:
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type="missing_cik",
                issue_detail=settings.missing_cik_issue_detail,
                load_stage=settings.load_stage,
                severity="warning",
            )
        if company.calibration_cohort_id == unassigned_cohort_id:
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=cohort_source_id,
                issue_type=settings.unassigned_issue_type,
                issue_detail=settings.unassigned_issue_detail,
                load_stage=settings.load_stage,
                severity="warning",
            )
        if company.investability_status.startswith("non_investable_"):
            add_issue(
                conn,
                ticker=company.ticker,
                company_id=company_id,
                source_id=seed_source_id,
                issue_type=company.investability_status,
                issue_detail=(
                    f"Excluded from active investable universe because "
                    f"listing_status={company.listing_status!r} security_type={company.security_type!r}."
                ),
                load_stage=settings.load_stage,
                severity="error",
            )
    return len(companies)


def run_universe_load(settings: UniverseLoadSettings, argv: list[str] | None = None) -> None:
    configure_utc_logging()
    args = parse_args(settings, argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = (
        args.universe_csv.expanduser().resolve()
        if args.universe_csv
        else resolve_optional_path(cfg_get(config, "technology_universe.seed_csv"), base_dir=base_dir)
    )
    policy_path = (
        args.policy.expanduser().resolve()
        if args.policy
        else resolve_optional_path(cfg_get(config, "technology_universe.policy_path"), base_dir=base_dir)
    )
    cohort_path = (
        args.cohorts.expanduser().resolve()
        if args.cohorts
        else resolve_optional_path(cfg_get(config, "technology_universe.cohort_path"), base_dir=base_dir)
    )
    model_family = str(args.model_family or cfg_get(config, "technology_universe.initial_subsector", settings.default_model_family)).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")

    policy = load_yaml_map(policy_path)
    unassigned_cohort_id = str(policy.get("default_unassigned_cohort_id") or settings.default_unassigned_cohort_id).strip()
    cohort_map = load_cohort_assignments(
        cohort_path,
        expected_model_family=model_family,
        cohort_label=settings.cohort_label,
    )
    companies = parse_universe_rows(
        universe_csv,
        policy=policy,
        cohort_map=cohort_map,
        model_family=model_family,
        settings=settings,
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        if not bool(args.skip_source_registry):
            source_registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            sources = load_source_registry(source_registry_path)
            upsert_source_registry(conn, sources)
        run_id = start_run(conn, run_type=settings.load_stage, input_path=universe_csv)
        try:
            with conn:
                row_count = upsert_universe(
                    conn,
                    companies,
                    settings=settings,
                    unassigned_cohort_id=unassigned_cohort_id,
                )
            missing_cik = sum(1 for company in companies if not company.cik)
            unassigned = sum(1 for company in companies if company.calibration_cohort_id == unassigned_cohort_id)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=row_count,
                message=(
                    f"model_family={model_family} rows={row_count} "
                    f"missing_cik={missing_cik} unassigned_cohort={unassigned}"
                ),
            )
            LOGGER.info("Loaded technology universe: db=%s model_family=%s rows=%d", db_path, model_family, row_count)
            LOGGER.info("Missing CIK rows: %d", missing_cik)
            LOGGER.info("Unassigned cohort rows: %d", unassigned)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
