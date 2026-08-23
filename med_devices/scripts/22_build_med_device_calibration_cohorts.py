#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db, quote_identifier, utc_now  # noqa: E402
from med_devices.core.fda_states import REGULATORY_MODEL_FDA_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.point_in_time import parse_iso_date, row_is_effective_asof, row_value  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOGGER = logging.getLogger("build_med_device_calibration_cohorts")
PIT_EXCLUDED_MEMBERSHIP_STATUSES = ("rejected", "candidate", "excluded")
FIELDNAMES = [
    "company_id",
    "model_family",
    "ticker",
    "company_name",
    "primary_subsector_raw",
    "calibration_cohort",
    "reimbursement_model",
    "regulatory_model",
    "business_model",
    "procedure_sensitivity",
    "capital_equipment_flag",
    "consumables_flag",
    "diagnostics_flag",
    "implantable_flag",
    "single_product_risk_flag",
    "taxonomy_confidence",
    "taxonomy_source",
    "valid_from",
    "valid_to",
    "reviewed_at",
    "analyst_reviewed",
    "updated_at",
]
FALLBACK_REVIEW_FIELDNAMES = [
    "ticker",
    "company_name",
    "primary_subsector_raw",
    "calibration_cohort",
    "reimbursement_model",
    "taxonomy_confidence",
    "taxonomy_source",
]
TAXONOMY_OVERRIDE_FIELDNAMES = [
    "ticker",
    "company_name",
    "calibration_cohort",
    "include_in_universe",
    "reason",
]


DIAGNOSTICS_CLINICAL_TESTS = {
    "ADPT",
    "BDSX",
    "BIAF",
    "BLLN",
    "CDNA",
    "CODX",
    "CSTL",
    "DGX",
    "FLGT",
    "FRNM",
    "GH",
    "GRAL",
    "IMDX",
    "LH",
    "MDAI",
    "MYGN",
    "NEO",
    "NTRA",
    "OPK",
    "OSUR",
    "PSNL",
    "PRPO",
    "QDEL",
    "VCYT",
    "VNRX",
    "WGS",
    "XGN",
}
LIFE_SCIENCE_TOOLS = {
    "A",
    "ATR",
    "AVTR",
    "AZTA",
    "BIO",
    "BLFS",
    "BRKR",
    "CRL",
    "CTKB",
    "DHR",
    "FEED",
    "IDXX",
    "LAB",
    "MASS",
    "MTD",
    "NEOG",
    "PACB",
    "PDEX",
    "QSI",
    "QTRX",
    "RGEN",
    "RVTY",
    "TMO",
    "TWST",
    "UFPT",
    "WAT",
}
HEALTHCARE_SERVICES = {"AHCO", "FMS", "IQV", "MEDP", "RDNT", "SHC", "VMD", "XWEL"}
DIABETES_WEARABLES_DRUG_DELIVERY = {"BBNX", "DXCM", "GCTK", "PODD", "SENS", "TNDM", "VTAK"}
SURGICAL_ROBOTICS_PLATFORMS = {"ISRG", "MBOT", "PRCT", "TMDX"}
CAPITAL_EQUIPMENT_IMAGING = {
    "BFLY",
    "GEHC",
    "ILMN",
    "MASI",
    "NVCR",
    "PHG",
    "SOLV",
    "STE",
    "VREX",
}
ORTHOPEDICS_SPINE_DENTAL = {
    "ALGN",
    "ATEC",
    "AXGN",
    "BLCO",
    "ENOV",
    "GKOS",
    "GMED",
    "KIDS",
    "NVST",
    "OFIX",
    "RXST",
    "XRAY",
    "ZBH",
}
HOSPITAL_SUPPLIES_CONSUMABLES_DME = {
    "AVNS",
    "BAX",
    "BDX",
    "COO",
    "ELMD",
    "ICUI",
    "INFU",
    "INGN",
    "ITGR",
    "RMD",
    "SMTI",
    "WRBY",
    "WST",
}
IMPLANTABLE_INTERVENTIONAL = {
    "ABT",
    "ANG",
    "ANGO",
    "ATRC",
    "AVR",
    "BSX",
    "CBLL",
    "CLPT",
    "CVRX",
    "DCTH",
    "EW",
    "HAE",
    "IART",
    "INSP",
    "IRTC",
    "LMAT",
    "LIVN",
    "MDT",
    "MMSI",
    "PEN",
    "PLSE",
    "SNWV",
    "SYK",
    "TFX",
}
CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS = "capital_equipment_procedure_platforms"
HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY = "home_chronic_care_devices_dme_drug_delivery"
HEALTHCARE_SERVICES_CRO_LAB_SERVICES = "healthcare_services_cro_lab_services"
HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM = "hospital_supplies_surgical_consumables_oem"
IMPLANTABLE_DIRECT_PAYMENT_COHORT = "implantable_interventional_devices_direct_payment"
IMPLANTABLE_PROCEDURE_BUNDLED_COHORT = "implantable_interventional_devices_procedure_bundled"
EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS = "emerging_single_product_therapeutic_platforms"
IMPLANTABLE_MIXED_OTHER_COHORT = EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS
ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES = "elective_vision_dental_aesthetic_devices"
ORTHOPEDICS_SPINE_SPORTS_IMPLANTS = "orthopedics_spine_sports_implants"
ELECTIVE_VISION_DENTAL = {"LNSR"}
VALID_CALIBRATION_COHORTS = {
    "diagnostics_clinical_tests",
    "life_science_tools_research_instruments",
    CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS,
    HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY,
    HEALTHCARE_SERVICES_CRO_LAB_SERVICES,
    HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM,
    IMPLANTABLE_DIRECT_PAYMENT_COHORT,
    IMPLANTABLE_PROCEDURE_BUNDLED_COHORT,
    ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES,
    EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS,
    ORTHOPEDICS_SPINE_SPORTS_IMPLANTS,
}
SINGLE_PRODUCT_RISK = {
    "ALMR",
    "AVR",
    "BBNX",
    "BFLY",
    "CATX",
    "CLPT",
    "CVRX",
    "DCTH",
    "GCTK",
    "GRAL",
    "FRNM",
    "INSP",
    "MBOT",
    "MOBI",
    "NVCR",
    "PLSE",
    "PROF",
    "SENS",
    "SNWV",
    "STIM",
    "TLSI",
    "TMDX",
    "VTAK",
}
TICKER_HEURISTIC_UNIVERSE_TICKERS = (
    DIAGNOSTICS_CLINICAL_TESTS
    | LIFE_SCIENCE_TOOLS
    | HEALTHCARE_SERVICES
    | DIABETES_WEARABLES_DRUG_DELIVERY
    | SURGICAL_ROBOTICS_PLATFORMS
    | CAPITAL_EQUIPMENT_IMAGING
    | ORTHOPEDICS_SPINE_DENTAL
    | HOSPITAL_SUPPLIES_CONSUMABLES_DME
    | IMPLANTABLE_INTERVENTIONAL
    | ELECTIVE_VISION_DENTAL
)


def is_implantable_interventional_cohort(cohort: str) -> bool:
    return cohort in {
        IMPLANTABLE_DIRECT_PAYMENT_COHORT,
        IMPLANTABLE_PROCEDURE_BUNDLED_COHORT,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device calibration cohorts and exposure tags.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument(
        "--active-universe",
        action="store_true",
        help="Use the current active universe for a recent live replay instead of PIT membership.",
    )
    return parser.parse_args()


def allow_missing_static_pit_metadata(config: dict[str, Any]) -> bool:
    return str(cfg_get(config, "historical_backfill.allow_missing_static_pit_metadata", True)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def create_taxonomy_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dim_company_model_taxonomy (
            company_id INTEGER PRIMARY KEY,
            model_family TEXT NOT NULL DEFAULT 'med_devices',
            ticker TEXT NOT NULL,
            company_name TEXT,
            primary_subsector_raw TEXT,
            calibration_cohort TEXT NOT NULL,
            reimbursement_model TEXT,
            regulatory_model TEXT,
            business_model TEXT,
            procedure_sensitivity TEXT,
            capital_equipment_flag INTEGER NOT NULL DEFAULT 0,
            consumables_flag INTEGER NOT NULL DEFAULT 0,
            diagnostics_flag INTEGER NOT NULL DEFAULT 0,
            implantable_flag INTEGER NOT NULL DEFAULT 0,
            single_product_risk_flag INTEGER NOT NULL DEFAULT 0,
            taxonomy_confidence REAL NOT NULL DEFAULT 0.0,
            taxonomy_source TEXT,
            valid_from TEXT,
            valid_to TEXT,
            reviewed_at TEXT,
            analyst_reviewed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
        )
        """
    )
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(dim_company_model_taxonomy)").fetchall()}
    if "model_family" not in existing:
        conn.execute("ALTER TABLE dim_company_model_taxonomy ADD COLUMN model_family TEXT DEFAULT 'med_devices'")
    conn.execute(
        """
        UPDATE dim_company_model_taxonomy
        SET model_family = 'med_devices'
        WHERE model_family IS NULL OR TRIM(model_family) = ''
        """
    )
    if "company_name" not in existing:
        conn.execute("ALTER TABLE dim_company_model_taxonomy ADD COLUMN company_name TEXT")
    for column in ("valid_from", "valid_to", "reviewed_at"):
        if column not in existing:
            conn.execute(f"ALTER TABLE dim_company_model_taxonomy ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_model_taxonomy_cohort ON dim_company_model_taxonomy(calibration_cohort)")


def latest_feature_asof(conn: Any) -> str:
    max_asofs: list[str] = []
    for table in ("feature_reimbursement", "feature_fda_product_risk", "feature_financial_valuation"):
        table_name = quote_identifier(table)
        try:
            row = conn.execute(f"SELECT MAX(asof_date) AS asof_date FROM {table_name}").fetchone()
        except Exception:
            continue
        asof = str(row["asof_date"] or "") if row is not None else ""
        if asof:
            max_asofs.append(asof)
    return max(max_asofs) if max_asofs else ""


def latest_feature_rows(conn: Any, table: str, *, asof: str | None = None) -> dict[int, dict[str, Any]]:
    table_name = quote_identifier(table)
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
            SELECT
                f.*,
                ROW_NUMBER() OVER (
                    PARTITION BY f.company_id
                    ORDER BY f.asof_date DESC, f.rowid DESC
                ) AS _latest_rank
            FROM {table_name} f
            WHERE (? IS NULL OR f.asof_date <= ?)
        ) ranked
        WHERE _latest_rank = 1
        """,
        (asof, asof),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def load_taxonomy_overrides(
    path: Path | None,
    *,
    asof: str | None = None,
    include_missing_pit_metadata: bool = True,
) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Configured taxonomy override CSV does not exist: {path}")
    overrides: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        if not row_is_effective_asof(row, asof, include_missing=include_missing_pit_metadata):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        include_in_universe = str(row.get("include_in_universe") or "true").strip().lower()
        if include_in_universe in {"0", "false", "no", "n"}:
            continue
        cohort = str(row.get("calibration_cohort") or row.get("final_cohort") or "").strip()
        if cohort not in VALID_CALIBRATION_COHORTS:
            raise ValueError(
                f"Invalid calibration_cohort {cohort!r} for ticker {ticker} in {path}; "
                f"expected one of {sorted(VALID_CALIBRATION_COHORTS)}"
            )
        overrides[ticker] = {
            "calibration_cohort": cohort,
            "reason": str(row.get("reason") or "").strip(),
            "valid_from": row_value(row, "valid_from", "start_date"),
            "valid_to": row_value(row, "valid_to", "effective_to", "end_date"),
            "reviewed_at": row_value(row, "reviewed_at", "review_date", "source_reviewed_at"),
        }
    LOGGER.info("Loaded taxonomy overrides: path=%s rows=%d", path, len(overrides))
    return overrides


def flag(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def classify_cohort(ticker: str, raw_subsector: str) -> tuple[str, float, str]:
    if ticker in DIAGNOSTICS_CLINICAL_TESTS:
        return "diagnostics_clinical_tests", 0.92, "ticker_heuristic"
    if ticker in LIFE_SCIENCE_TOOLS:
        return "life_science_tools_research_instruments", 0.90, "ticker_heuristic"
    if ticker in HEALTHCARE_SERVICES:
        return HEALTHCARE_SERVICES_CRO_LAB_SERVICES, 0.88, "ticker_heuristic"
    if ticker in DIABETES_WEARABLES_DRUG_DELIVERY:
        return HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY, 0.92, "ticker_heuristic"
    if ticker in SURGICAL_ROBOTICS_PLATFORMS:
        return CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS, 0.88, "ticker_heuristic"
    if ticker in CAPITAL_EQUIPMENT_IMAGING:
        return CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS, 0.88, "ticker_heuristic"
    if ticker in ORTHOPEDICS_SPINE_DENTAL:
        return ORTHOPEDICS_SPINE_SPORTS_IMPLANTS, 0.88, "ticker_heuristic"
    if ticker in ELECTIVE_VISION_DENTAL:
        return ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES, 0.90, "ticker_heuristic"
    if ticker in HOSPITAL_SUPPLIES_CONSUMABLES_DME:
        return HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM, 0.86, "ticker_heuristic"
    if ticker in IMPLANTABLE_INTERVENTIONAL:
        return IMPLANTABLE_DIRECT_PAYMENT_COHORT, 0.86, "ticker_heuristic"

    text = raw_subsector.lower()
    if "diagnostic" in text:
        return "diagnostics_clinical_tests", 0.70, "subsector_fallback"
    if "facility" in text or "care" in text:
        return HEALTHCARE_SERVICES_CRO_LAB_SERVICES, 0.65, "subsector_fallback"
    if "instrument" in text or "supplies" in text:
        return HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM, 0.65, "subsector_fallback"
    return EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS, 0.55, "default_med_device"


def reimbursement_model(row: dict[str, Any] | None, cohort: str) -> str:
    if row is None:
        return "unknown"
    status = str(row.get("reimbursement_status") or "").strip()
    if flag(row.get("diagnostics_lab_flag")) or cohort == "diagnostics_clinical_tests":
        return "diagnostics_lab"
    if flag(row.get("capital_equipment_flag")) or cohort == CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS:
        return "capital_equipment"
    if flag(row.get("procedure_bundled_flag")):
        return "procedure_bundled"
    if flag(row.get("payment_rate_evidence")) or flag(row.get("direct_code_evidence")):
        return "direct_payment"
    if flag(row.get("coverage_policy_evidence")):
        return "coverage_policy"
    if "exempt" in status.lower() or "not_applicable" in status.lower():
        return "not_applicable"
    return "unknown"


def validate_final_rows(rows: list[dict[str, Any]]) -> None:
    invalid = [
        f"{row.get('ticker')}->{row.get('calibration_cohort')}"
        for row in rows
        if str(row.get("calibration_cohort") or "") not in VALID_CALIBRATION_COHORTS
    ]
    if invalid:
        examples = ", ".join(invalid[:25])
        raise ValueError(
            "Invalid calibration cohorts produced before persistence. "
            f"Examples: {examples}; expected one of {sorted(VALID_CALIBRATION_COHORTS)}"
        )


def regulatory_model(row: dict[str, Any] | None, ticker: str, cohort: str) -> str:
    if row is not None:
        state = str(row.get("fda_review_state") or row.get("review_adjusted_fda_state") or "").strip().lower()
        if state in REGULATORY_MODEL_FDA_STATES:
            return state
    if cohort in {"life_science_tools_research_instruments", HEALTHCARE_SERVICES_CRO_LAB_SERVICES}:
        return "low_fda_exposure"
    if cohort == EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS:
        return "development_stage_fda"
    if ticker in {"AVR", "GCTK", "MBOT", "PLSE", "CATX"}:
        return "development_stage_fda"
    if is_implantable_interventional_cohort(cohort):
        return "recall_sensitive"
    return "fda_510k_heavy"


def business_model(cohort: str) -> str:
    return {
        IMPLANTABLE_DIRECT_PAYMENT_COHORT: "procedure_volume_sensitive_direct_payment",
        IMPLANTABLE_PROCEDURE_BUNDLED_COHORT: "procedure_volume_sensitive_bundled",
        EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS: "single_product_binary_therapeutic_platform",
        ORTHOPEDICS_SPINE_SPORTS_IMPLANTS: "orthopedics_spine_sports_implants",
        ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES: "elective_vision_dental_aesthetic_devices",
        HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY: "home_chronic_care_dme_drug_delivery",
        HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM: "hospital_supplies_surgical_consumables_oem",
        "diagnostics_clinical_tests": "test_volume_reimbursement",
        "life_science_tools_research_instruments": "research_capex_consumables",
        CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS: "capital_equipment_procedure_platform",
        HEALTHCARE_SERVICES_CRO_LAB_SERVICES: "service_revenue_lab_cro",
    }.get(cohort, "mixed_medtech")


def procedure_sensitivity(cohort: str) -> str:
    if cohort in {"life_science_tools_research_instruments", HEALTHCARE_SERVICES_CRO_LAB_SERVICES}:
        return "low"
    if cohort == EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS:
        return "binary_event_sensitive"
    if cohort in {HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY, ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES}:
        return "medium"
    return "high"


def build_rows(
    conn: Any,
    *,
    taxonomy_overrides: dict[str, dict[str, str]],
    asof: str | None = None,
    include_active_universe: bool = False,
) -> list[dict[str, Any]]:
    reimbursement = latest_feature_rows(conn, "feature_reimbursement", asof=asof)
    fda = latest_feature_rows(conn, "feature_fda_product_risk", asof=asof)
    if asof and not include_active_universe:
        status_placeholders = ", ".join("?" for _ in PIT_EXCLUDED_MEMBERSHIP_STATUSES)
        companies = conn.execute(
            f"""
            SELECT c.company_id, c.ticker, c.company_name, c.subsector
            FROM dim_company c
            WHERE EXISTS (
                SELECT 1
                FROM dim_universe_membership m
                WHERE m.company_id = c.company_id
                  AND m.model_family = 'med_devices'
                  AND m.point_in_time_flag = 1
                  AND LOWER(COALESCE(m.membership_status, '')) NOT IN ({status_placeholders})
                  AND m.start_date <= ?
                  AND (m.end_date IS NULL OR m.end_date >= ?)
            )
            ORDER BY c.ticker
            """,
            (*PIT_EXCLUDED_MEMBERSHIP_STATUSES, asof, asof),
        ).fetchall()
    else:
        companies = conn.execute(
            """
            SELECT company_id, ticker, company_name, subsector
            FROM dim_company
            WHERE is_active = 1
            ORDER BY ticker
            """
        ).fetchall()
    now = utc_now()
    out: list[dict[str, Any]] = []
    for company in companies:
        ticker = str(company["ticker"] or "").upper()
        raw_subsector = str(company["subsector"] or "")
        override = taxonomy_overrides.get(ticker)
        if override is not None:
            cohort = override["calibration_cohort"]
            confidence = 0.98
            source = "manual_taxonomy_override"
            analyst_reviewed = 1
            valid_from = override.get("valid_from", "")
            valid_to = override.get("valid_to", "")
            reviewed_at = override.get("reviewed_at", "")
            reimb_model = reimbursement_model(reimbursement.get(int(company["company_id"])), cohort)
        else:
            cohort, confidence, source = classify_cohort(ticker, raw_subsector)
            reimb_model = reimbursement_model(reimbursement.get(int(company["company_id"])), cohort)
            analyst_reviewed = 0
            valid_from = ""
            valid_to = ""
            reviewed_at = ""
        row = {
            "company_id": int(company["company_id"]),
            "model_family": "med_devices",
            "ticker": ticker,
            "company_name": company["company_name"] or "",
            "primary_subsector_raw": raw_subsector,
            "calibration_cohort": cohort,
            "reimbursement_model": reimb_model,
            "regulatory_model": regulatory_model(fda.get(int(company["company_id"])), ticker, cohort),
            "business_model": business_model(cohort),
            "procedure_sensitivity": procedure_sensitivity(cohort),
            "capital_equipment_flag": 1 if cohort == CAPITAL_EQUIPMENT_PROCEDURE_PLATFORMS else 0,
            "consumables_flag": 1
            if cohort in {HOSPITAL_SUPPLIES_SURGICAL_CONSUMABLES_OEM, HOME_CHRONIC_CARE_DEVICES_DME_DRUG_DELIVERY}
            else 0,
            "diagnostics_flag": 1 if cohort == "diagnostics_clinical_tests" else 0,
            "implantable_flag": 1
            if is_implantable_interventional_cohort(cohort)
            or cohort in {ORTHOPEDICS_SPINE_SPORTS_IMPLANTS, ELECTIVE_VISION_DENTAL_AESTHETIC_DEVICES}
            else 0,
            "single_product_risk_flag": 1
            if ticker in SINGLE_PRODUCT_RISK or cohort == EMERGING_SINGLE_PRODUCT_THERAPEUTIC_PLATFORMS
            else 0,
            "taxonomy_confidence": confidence,
            "taxonomy_source": source,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "reviewed_at": reviewed_at,
            "analyst_reviewed": analyst_reviewed,
            "updated_at": now,
        }
        out.append(row)
    return out


def fallback_ticker_heuristic_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("ticker") or "").upper() not in TICKER_HEURISTIC_UNIVERSE_TICKERS
        and int(row.get("analyst_reviewed") or 0) == 0
    ]


def warn_on_unmapped_ticker_heuristics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fallback_rows = fallback_ticker_heuristic_rows(rows)
    if not fallback_rows:
        return []
    examples = ", ".join(
        f"{row.get('ticker')}->{row.get('calibration_cohort')}({row.get('taxonomy_source')})"
        for row in fallback_rows[:25]
    )
    LOGGER.warning(
        "Calibration cohort ticker heuristic has no explicit mapping for %d active tickers; "
        "fallback taxonomy was used. Examples: %s",
        len(fallback_rows),
        examples,
    )
    return fallback_rows


def warn_on_unmatched_taxonomy_overrides(
    rows: list[dict[str, Any]],
    taxonomy_overrides: dict[str, dict[str, str]],
) -> list[str]:
    active_tickers = {str(row.get("ticker") or "").upper() for row in rows}
    missing = sorted(set(taxonomy_overrides) - active_tickers)
    if missing:
        LOGGER.warning(
            "Taxonomy override CSV contains %d ticker(s) not present in active dim_company universe: %s",
            len(missing),
            ", ".join(missing[:50]),
        )
    return missing


def write_fallback_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FALLBACK_REVIEW_FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(FIELDNAMES)
    placeholders = ", ".join("?" for _ in columns)
    update_parts: list[str] = []
    for column in columns:
        if column == "company_id":
            continue
        update_parts.append(f"{column} = excluded.{column}")
    updates = ", ".join(update_parts)
    conn.executemany(
        f"""
        INSERT INTO dim_company_model_taxonomy({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(company_id) DO UPDATE SET {updates}
        """,
        [tuple(row[column] for column in columns) for row in rows],
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "calibration.taxonomy_output_csv"), base_dir=base_dir)
    )
    override_raw = str(cfg_get(config, "calibration.taxonomy_override_csv", "") or "").strip()
    override_csv = resolve_path(override_raw, base_dir=base_dir) if override_raw else None
    include_missing_pit_metadata = allow_missing_static_pit_metadata(config)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        create_taxonomy_table(conn)
        asof_text = args.asof.strip() if args.asof else latest_feature_asof(conn)
        parsed_asof = parse_iso_date(asof_text)
        if parsed_asof is None:
            LOGGER.warning("Unable to infer calibration cohort as-of date from feature tables.")
            raise ValueError("No as-of date supplied and no feature rows are available to infer one.")
        asof_text = parsed_asof.isoformat()
        if args.active_universe:
            replay_window_days = int(cfg_get(config, "scoring.oos_replay_window_days", 5))
            asof_age_days = (datetime.now(timezone.utc).date() - parsed_asof).days
            if asof_age_days < 0 or asof_age_days > replay_window_days:
                raise ValueError(
                    "--active-universe is restricted to recent live replays: "
                    f"asof={asof_text} age_days={asof_age_days} max_age_days={replay_window_days}"
                )
        taxonomy_overrides = load_taxonomy_overrides(
            override_csv,
            asof=asof_text,
            include_missing_pit_metadata=include_missing_pit_metadata,
        )
        rows = build_rows(
            conn,
            taxonomy_overrides=taxonomy_overrides,
            asof=asof_text,
            include_active_universe=args.active_universe,
        )
        validate_final_rows(rows)
        missing_override_tickers = warn_on_unmatched_taxonomy_overrides(rows, taxonomy_overrides)
        fallback_rows = warn_on_unmapped_ticker_heuristics(rows)
        upsert_rows(conn, rows)
    write_csv(output_csv, rows)
    fallback_review_csv = output_csv.with_name(f"{output_csv.stem}_fallback_review.csv")
    write_fallback_review_csv(fallback_review_csv, fallback_rows)
    cohorts = sorted({row["calibration_cohort"] for row in rows})
    print(
        f"taxonomy_csv={output_csv} rows={len(rows)} cohorts={len(cohorts)} "
        f"fallback_review_csv={fallback_review_csv} fallback_rows={len(fallback_rows)} "
        f"missing_override_tickers={len(missing_override_tickers)}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
