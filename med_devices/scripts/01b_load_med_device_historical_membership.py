#!/usr/bin/env python3
"""Load point-in-time historical/delisted med-device calibration members."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("load_med_device_historical_membership")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_MEMBERSHIP_CSV = PACKAGE_ROOT / "data" / "med_device_historical_membership.csv"
MODEL_FAMILY = "med_devices"
RUN_TYPE = "load_med_device_historical_membership"


VALID_COHORTS = {
    "capital_equipment_procedure_platforms",
    "diagnostics_clinical_tests",
    "elective_vision_dental_aesthetic_devices",
    "emerging_single_product_medtech_platforms",
    "healthcare_services_cro_lab_services",
    "home_chronic_care_devices_dme_drug_delivery",
    "hospital_supplies_surgical_consumables_oem",
    "implantable_interventional_devices_direct_payment",
    "implantable_interventional_devices_procedure_bundled",
    "life_science_tools_research_instruments",
    "orthopedics_spine_sports_implants",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load med-devices historical/delisted PIT membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--membership-csv", type=Path, default=DEFAULT_MEMBERSHIP_CSV)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


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
    seen: set[tuple[str, str]] = set()
    for row in rows:
        ticker = normalize_ticker(row.get("ticker") or row.get("internal_ticker"))
        if not ticker:
            continue
        cohort = str(row.get("calibration_cohort") or "").strip()
        if cohort not in VALID_COHORTS:
            raise ValueError(f"{ticker}: invalid calibration_cohort={cohort!r}")
        start_date = parse_date_text(row.get("start_date"), field="start_date", ticker=ticker)
        end_date = parse_date_text(row.get("end_date"), field="end_date", ticker=ticker)
        period_key = (ticker, start_date)
        if period_key in seen:
            raise ValueError(f"Duplicate historical membership interval: {ticker} start_date={start_date}")
        seen.add(period_key)
        if end_date < start_date:
            raise ValueError(f"{ticker}: end_date {end_date} precedes start_date {start_date}")
        row["ticker"] = ticker
        row["internal_ticker"] = ticker
        row["exchange_ticker"] = normalize_ticker(row.get("exchange_ticker")) or ticker
        row["start_date"] = start_date
        row["end_date"] = end_date
        row["membership_status"] = str(row.get("membership_status") or "historical").strip().lower()
        row["confidence"] = str(float(row.get("confidence") or 0.75))
        out.append(row)
    if not out:
        raise ValueError(f"No historical membership rows found in {path}")
    return out


def upsert_source_registry(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_owner, source_type, base_url, documentation_url,
            authentication_required, free_key_required, api_key_env, rate_limit_notes, refresh_frequency,
            terms_url, data_owner, raw_schema, staging_tables, canonical_tables, feature_stages,
            priority, status, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            stage = excluded.stage,
            source_name = excluded.source_name,
            source_owner = excluded.source_owner,
            source_type = excluded.source_type,
            base_url = excluded.base_url,
            documentation_url = excluded.documentation_url,
            authentication_required = excluded.authentication_required,
            free_key_required = excluded.free_key_required,
            rate_limit_notes = excluded.rate_limit_notes,
            refresh_frequency = excluded.refresh_frequency,
            terms_url = excluded.terms_url,
            data_owner = excluded.data_owner,
            raw_schema = excluded.raw_schema,
            staging_tables = excluded.staging_tables,
            canonical_tables = excluded.canonical_tables,
            feature_stages = excluded.feature_stages,
            priority = excluded.priority,
            status = excluded.status,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            source_id,
            "calibration_membership",
            "Med-devices historical membership seed",
            "Analyst curated",
            "local_csv",
            "file://med_devices/data/med_device_historical_membership.csv",
            "",
            0,
            0,
            "",
            "Manual curation; update only through review.",
            "manual_as_needed",
            "",
            "Analyst curated",
            "Calibration-only point-in-time historical/delisted membership rows.",
            "dim_universe_membership",
            "dim_company,dim_security,dim_company_model_taxonomy,dim_universe_membership",
            "historical_backfill,calibration,backtest",
            20,
            "active",
            "Historical/delisted calibration members are inactive and must not enter live production output.",
            now,
            now,
        ),
    )


def insert_identifier(conn: Any, *, company_id: int, identifier_type: str, identifier_value: str, source_id: str, confidence: float) -> None:
    value = str(identifier_value or "").strip()
    if not value:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_identifier(
            company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
            company_id = excluded.company_id,
            source_id = excluded.source_id,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (company_id, identifier_type, value, source_id, confidence, now, now),
    )


def business_model(cohort: str) -> str:
    return {
        "capital_equipment_procedure_platforms": "capital_equipment_procedure_platform",
        "diagnostics_clinical_tests": "test_volume_reimbursement",
        "hospital_supplies_surgical_consumables_oem": "hospital_supplies_surgical_consumables_oem",
        "implantable_interventional_devices_direct_payment": "procedure_volume_sensitive_direct_payment",
        "orthopedics_spine_sports_implants": "orthopedics_spine_sports_implants",
    }.get(cohort, "historical_medtech_calibration_member")


def procedure_sensitivity(cohort: str) -> str:
    if cohort == "diagnostics_clinical_tests":
        return "medium"
    if cohort == "hospital_supplies_surgical_consumables_oem":
        return "medium"
    return "high"


def single_product_risk_flag(cohort: str) -> int:
    return int(cohort == "emerging_single_product_medtech_platforms")


def upsert_historical_member(conn: Any, row: dict[str, str], *, source_id: str) -> int:
    now = utc_now()
    ticker = normalize_ticker(row["ticker"])
    company_name = str(row.get("company_name") or ticker).strip()
    exchange = str(row.get("exchange") or "").strip()
    country = str(row.get("country") or "United States Of America").strip()
    currency = str(row.get("currency") or "USD").strip()
    security_type = str(row.get("security_type") or "Common Stock").strip()
    cohort = str(row["calibration_cohort"]).strip()
    confidence = float(row.get("confidence") or 0.75)
    cik = str(row.get("cik") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    notes = str(row.get("notes") or "").strip()

    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, exchange, sector, industry, subsector, country, currency,
            universe_status, is_active, medtech_pure_play_flag, data_quality_status, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'Healthcare', 'Medical Devices', ?, ?, ?,
                'historical', 0, 1, 'historical_membership_seed', ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = COALESCE(NULLIF(excluded.cik, ''), dim_company.cik),
            company_name = COALESCE(NULLIF(excluded.company_name, ''), dim_company.company_name),
            exchange = COALESCE(NULLIF(dim_company.exchange, ''), excluded.exchange),
            sector = COALESCE(NULLIF(dim_company.sector, ''), excluded.sector),
            industry = COALESCE(NULLIF(dim_company.industry, ''), excluded.industry),
            subsector = COALESCE(NULLIF(dim_company.subsector, ''), excluded.subsector),
            country = COALESCE(NULLIF(dim_company.country, ''), excluded.country),
            currency = COALESCE(NULLIF(dim_company.currency, ''), excluded.currency),
            universe_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.universe_status ELSE excluded.universe_status END,
            is_active = CASE WHEN dim_company.is_active = 1 THEN dim_company.is_active ELSE excluded.is_active END,
            medtech_pure_play_flag = CASE WHEN dim_company.is_active = 1 THEN dim_company.medtech_pure_play_flag ELSE excluded.medtech_pure_play_flag END,
            data_quality_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.data_quality_status ELSE excluded.data_quality_status END,
            updated_at = excluded.updated_at
        """,
        (ticker, cik, company_name, exchange, cohort, country, currency, now, now),
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
    insert_identifier(conn, company_id=company_id, identifier_type="CIK", identifier_value=cik, source_id=source_id, confidence=0.85)
    insert_identifier(
        conn,
        company_id=company_id,
        identifier_type="EXCHANGE_TICKER",
        identifier_value=str(row.get("exchange_ticker") or ticker),
        source_id=source_id,
        confidence=0.90,
    )
    insert_identifier(
        conn,
        company_id=company_id,
        identifier_type="NORGATE_SOURCE_SYMBOL",
        identifier_value=str(row.get("price_source_symbol") or ""),
        source_id=source_id,
        confidence=0.95,
    )

    conn.execute(
        """
        INSERT INTO dim_company_model_taxonomy(
            company_id, model_family, ticker, company_name, primary_subsector_raw, calibration_cohort,
            reimbursement_model, regulatory_model, business_model, procedure_sensitivity,
            capital_equipment_flag, consumables_flag, diagnostics_flag, implantable_flag,
            single_product_risk_flag, taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'historical_unknown', 'historical_fda_exposure',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            model_family = excluded.model_family,
            ticker = excluded.ticker,
            company_name = excluded.company_name,
            primary_subsector_raw = excluded.primary_subsector_raw,
            calibration_cohort = excluded.calibration_cohort,
            reimbursement_model = excluded.reimbursement_model,
            regulatory_model = excluded.regulatory_model,
            business_model = excluded.business_model,
            procedure_sensitivity = excluded.procedure_sensitivity,
            capital_equipment_flag = excluded.capital_equipment_flag,
            consumables_flag = excluded.consumables_flag,
            diagnostics_flag = excluded.diagnostics_flag,
            implantable_flag = excluded.implantable_flag,
            single_product_risk_flag = excluded.single_product_risk_flag,
            taxonomy_confidence = excluded.taxonomy_confidence,
            taxonomy_source = excluded.taxonomy_source,
            analyst_reviewed = excluded.analyst_reviewed,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            MODEL_FAMILY,
            ticker,
            company_name,
            cohort,
            cohort,
            business_model(cohort),
            procedure_sensitivity(cohort),
            1 if cohort == "capital_equipment_procedure_platforms" else 0,
            1 if cohort == "hospital_supplies_surgical_consumables_oem" else 0,
            1 if cohort == "diagnostics_clinical_tests" else 0,
            1 if cohort in {"implantable_interventional_devices_direct_payment", "orthopedics_spine_sports_implants"} else 0,
            single_product_risk_flag(cohort),
            confidence,
            source_id,
            now,
        ),
    )

    reason = ";".join(part for part in [str(row.get("event_type") or ""), str(row.get("successor_ticker") or ""), notes] if part)
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id, membership_basis,
            start_date, end_date, membership_status, is_current_member,
            point_in_time_flag, confidence, reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'calibration_only_historical_delisted',
                ?, ?, ?, 0, 1, ?, ?, ?, ?)
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
            MODEL_FAMILY,
            source_id,
            row["start_date"],
            row["end_date"],
            str(row.get("membership_status") or "historical"),
            confidence,
            f"{reason};source={source_url}" if source_url else reason,
            now,
            now,
        ),
    )
    return company_id


def deactivate_removed_memberships(conn: Any, *, source_id: str, active_intervals: set[tuple[str, str]]) -> int:
    """Disable stale calibration-only PIT intervals that are no longer in the source CSV."""
    if not active_intervals:
        return 0
    existing = conn.execute(
        """
        SELECT ticker, start_date
        FROM dim_universe_membership
        WHERE model_family = ?
          AND membership_source_id = ?
          AND membership_basis = 'calibration_only_historical_delisted'
          AND point_in_time_flag = 1
        """,
        (MODEL_FAMILY, source_id),
    ).fetchall()
    stale_intervals = [
        (str(row["ticker"]).upper(), str(row["start_date"]))
        for row in existing
        if (str(row["ticker"]).upper(), str(row["start_date"])) not in active_intervals
    ]
    if not stale_intervals:
        return 0
    now = utc_now()
    before = conn.total_changes
    for ticker, start_date in stale_intervals:
        conn.execute(
            """
            UPDATE dim_universe_membership
            SET membership_status = 'removed_from_source',
                point_in_time_flag = 0,
                is_current_member = 0,
                confidence = 0.0,
                reason = COALESCE(reason, '') || ';removed_from_current_historical_membership_seed',
                updated_at = ?
            WHERE model_family = ?
              AND membership_source_id = ?
              AND membership_basis = 'calibration_only_historical_delisted'
              AND ticker = ?
              AND start_date = ?
            """,
            (now, MODEL_FAMILY, source_id, ticker, start_date),
        )
    return conn.total_changes - before


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, "med_devices_universe.historical_membership_source_id", "med_device_historical_membership_seed"))
    membership_csv = args.membership_csv.expanduser().resolve()
    if not membership_csv.exists():
        membership_csv = resolve_path(
            cfg_get(config, "med_devices_universe.historical_membership_csv", "data/med_device_historical_membership.csv"),
            base_dir=base_dir,
        )
    rows = read_membership_csv(membership_csv)
    if args.dry_run:
        cohorts = sorted({row["calibration_cohort"] for row in rows})
        print(f"historical_membership_csv={membership_csv} rows={len(rows)} cohorts={len(cohorts)} dry_run=1")
        return 0

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, source_id)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=membership_csv)
        try:
            active_intervals = {(row["ticker"], row["start_date"]) for row in rows}
            for row in rows:
                upsert_historical_member(conn, row, source_id=source_id)
            deactivated = deactivate_removed_memberships(
                conn,
                source_id=source_id,
                active_intervals=active_intervals,
            )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(rows),
                message=f"source_id={source_id};deactivated_removed={deactivated}",
            )
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=repr(exc))
            raise
    cohorts = sorted({row["calibration_cohort"] for row in rows})
    print(f"historical_membership_csv={membership_csv} rows={len(rows)} cohorts={len(cohorts)} source_id={source_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
