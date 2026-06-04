#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
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


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "company_id",
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
    "analyst_reviewed",
    "updated_at",
]


DIAGNOSTICS_CLINICAL_TESTS = {
    "ADPT",
    "BDSX",
    "BIAF",
    "CDNA",
    "CODX",
    "CSTL",
    "DGX",
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
DIABETES_WEARABLES_DRUG_DELIVERY = {"DXCM", "GCTK", "PODD", "SENS", "TNDM", "VTAK"}
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
IMPLANTABLE_DIRECT_PAYMENT_COHORT = "implantable_interventional_devices_direct_payment"
IMPLANTABLE_PROCEDURE_BUNDLED_COHORT = "implantable_interventional_devices_procedure_bundled"
IMPLANTABLE_MIXED_OTHER_COHORT = "implantable_interventional_devices_other"
SINGLE_PRODUCT_RISK = {
    "AVR",
    "BFLY",
    "CATX",
    "CLPT",
    "CVRX",
    "DCTH",
    "GCTK",
    "GRAL",
    "INSP",
    "MBOT",
    "NVCR",
    "PLSE",
    "SENS",
    "SNWV",
    "TMDX",
}


def is_implantable_interventional_cohort(cohort: str) -> bool:
    return cohort in {
        "implantable_interventional_devices",
        IMPLANTABLE_DIRECT_PAYMENT_COHORT,
        IMPLANTABLE_PROCEDURE_BUNDLED_COHORT,
        IMPLANTABLE_MIXED_OTHER_COHORT,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device calibration cohorts and exposure tags.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def create_taxonomy_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dim_company_model_taxonomy (
            company_id INTEGER PRIMARY KEY,
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
            analyst_reviewed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
        )
        """
    )
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(dim_company_model_taxonomy)").fetchall()}
    if "company_name" not in existing:
        conn.execute("ALTER TABLE dim_company_model_taxonomy ADD COLUMN company_name TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_model_taxonomy_cohort ON dim_company_model_taxonomy(calibration_cohort)")


def latest_feature_rows(conn: Any, table: str) -> dict[int, dict[str, Any]]:
    table_name = quote_identifier(table)
    rows = conn.execute(
        f"""
        SELECT f.*
        FROM {table_name} f
        WHERE f.rowid = (
            SELECT f2.rowid
            FROM {table_name} f2
            WHERE f2.company_id = f.company_id
            ORDER BY f2.asof_date DESC, f2.rowid DESC
            LIMIT 1
        )
        """
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def flag(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def classify_cohort(ticker: str, raw_subsector: str) -> tuple[str, float, str]:
    if ticker in DIAGNOSTICS_CLINICAL_TESTS:
        return "diagnostics_clinical_tests", 0.92, "ticker_heuristic"
    if ticker in LIFE_SCIENCE_TOOLS:
        return "life_science_tools_research_instruments", 0.90, "ticker_heuristic"
    if ticker in HEALTHCARE_SERVICES:
        return "healthcare_services_cro_other", 0.88, "ticker_heuristic"
    if ticker in DIABETES_WEARABLES_DRUG_DELIVERY:
        return "diabetes_wearables_drug_delivery", 0.92, "ticker_heuristic"
    if ticker in SURGICAL_ROBOTICS_PLATFORMS:
        return "surgical_robotics_platforms", 0.88, "ticker_heuristic"
    if ticker in CAPITAL_EQUIPMENT_IMAGING:
        return "capital_equipment_imaging_monitoring", 0.88, "ticker_heuristic"
    if ticker in ORTHOPEDICS_SPINE_DENTAL:
        return "orthopedics_spine_dental", 0.88, "ticker_heuristic"
    if ticker in HOSPITAL_SUPPLIES_CONSUMABLES_DME:
        return "hospital_supplies_consumables_dme", 0.86, "ticker_heuristic"
    if ticker in IMPLANTABLE_INTERVENTIONAL:
        return "implantable_interventional_devices", 0.86, "ticker_heuristic"

    text = raw_subsector.lower()
    if "diagnostic" in text:
        return "diagnostics_clinical_tests", 0.70, "subsector_fallback"
    if "facility" in text or "care" in text:
        return "healthcare_services_cro_other", 0.65, "subsector_fallback"
    if "instrument" in text or "supplies" in text:
        return "hospital_supplies_consumables_dme", 0.65, "subsector_fallback"
    return "implantable_interventional_devices", 0.55, "default_med_device"


def reimbursement_model(row: dict[str, Any] | None, cohort: str) -> str:
    if row is None:
        return "unknown"
    status = str(row.get("reimbursement_status") or "").strip()
    if flag(row.get("diagnostics_lab_flag")) or cohort == "diagnostics_clinical_tests":
        return "diagnostics_lab"
    if flag(row.get("capital_equipment_flag")) or cohort == "capital_equipment_imaging_monitoring":
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


def refine_calibration_cohort(
    base_cohort: str,
    reimb_model: str,
    confidence: float,
    source: str,
) -> tuple[str, float, str]:
    if base_cohort != "implantable_interventional_devices":
        return base_cohort, confidence, source
    if reimb_model == "direct_payment":
        return IMPLANTABLE_DIRECT_PAYMENT_COHORT, max(confidence, 0.88), f"{source}+reimbursement_model_split"
    if reimb_model == "procedure_bundled":
        return IMPLANTABLE_PROCEDURE_BUNDLED_COHORT, max(confidence, 0.86), f"{source}+reimbursement_model_split"
    if reimb_model == "diagnostics_lab":
        return "diagnostics_clinical_tests", max(confidence, 0.82), f"{source}+diagnostics_reclass"
    return IMPLANTABLE_MIXED_OTHER_COHORT, confidence, f"{source}+reimbursement_model_split"


def regulatory_model(row: dict[str, Any] | None, ticker: str, cohort: str) -> str:
    if row is not None:
        state = str(row.get("fda_review_state") or row.get("review_adjusted_fda_state") or "").strip().lower()
        if state in REGULATORY_MODEL_FDA_STATES:
            return state
    if cohort in {"life_science_tools_research_instruments", "healthcare_services_cro_other"}:
        return "low_fda_exposure"
    if ticker in {"AVR", "GCTK", "MBOT", "PLSE", "CATX"}:
        return "development_stage_fda"
    if is_implantable_interventional_cohort(cohort):
        return "recall_sensitive"
    return "fda_510k_heavy"


def business_model(cohort: str) -> str:
    return {
        "implantable_interventional_devices": "procedure_volume_sensitive",
        IMPLANTABLE_DIRECT_PAYMENT_COHORT: "procedure_volume_sensitive_direct_payment",
        IMPLANTABLE_PROCEDURE_BUNDLED_COHORT: "procedure_volume_sensitive_bundled",
        IMPLANTABLE_MIXED_OTHER_COHORT: "procedure_volume_sensitive_mixed",
        "orthopedics_spine_dental": "elective_procedure_sensitive",
        "surgical_robotics_platforms": "installed_base_platform",
        "diabetes_wearables_drug_delivery": "recurring_consumables",
        "hospital_supplies_consumables_dme": "consumables_or_dme",
        "diagnostics_clinical_tests": "test_volume_reimbursement",
        "life_science_tools_research_instruments": "research_capex_consumables",
        "capital_equipment_imaging_monitoring": "hospital_capex_cycle",
        "healthcare_services_cro_other": "service_revenue",
    }.get(cohort, "mixed_medtech")


def build_rows(conn: Any) -> list[dict[str, Any]]:
    reimbursement = latest_feature_rows(conn, "feature_reimbursement")
    fda = latest_feature_rows(conn, "feature_fda_product_risk")
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
        base_cohort, confidence, source = classify_cohort(ticker, raw_subsector)
        reimb_model = reimbursement_model(reimbursement.get(int(company["company_id"])), base_cohort)
        cohort, confidence, source = refine_calibration_cohort(base_cohort, reimb_model, confidence, source)
        row = {
            "company_id": int(company["company_id"]),
            "ticker": ticker,
            "company_name": company["company_name"] or "",
            "primary_subsector_raw": raw_subsector,
            "calibration_cohort": cohort,
            "reimbursement_model": reimb_model,
            "regulatory_model": regulatory_model(fda.get(int(company["company_id"])), ticker, cohort),
            "business_model": business_model(cohort),
            "procedure_sensitivity": "low" if cohort in {"life_science_tools_research_instruments", "healthcare_services_cro_other"} else "high",
            "capital_equipment_flag": 1 if cohort in {"capital_equipment_imaging_monitoring", "surgical_robotics_platforms"} else 0,
            "consumables_flag": 1 if cohort in {"hospital_supplies_consumables_dme", "diabetes_wearables_drug_delivery"} else 0,
            "diagnostics_flag": 1 if cohort == "diagnostics_clinical_tests" else 0,
            "implantable_flag": 1 if is_implantable_interventional_cohort(cohort) or cohort == "orthopedics_spine_dental" else 0,
            "single_product_risk_flag": 1 if ticker in SINGLE_PRODUCT_RISK else 0,
            "taxonomy_confidence": confidence,
            "taxonomy_source": source,
            "analyst_reviewed": 0,
            "updated_at": now,
        }
        out.append(row)
    return out


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(FIELDNAMES)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "company_id")
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
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        create_taxonomy_table(conn)
        rows = build_rows(conn)
        upsert_rows(conn, rows)
    write_csv(output_csv, rows)
    cohorts = sorted({row["calibration_cohort"] for row in rows})
    print(f"taxonomy_csv={output_csv} rows={len(rows)} cohorts={len(cohorts)}")


if __name__ == "__main__":
    main()
