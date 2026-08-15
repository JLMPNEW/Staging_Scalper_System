from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from med_devices.core.analyst_review import (
    AnalystReviewDecision,
    decision_lifecycle_rows,
    decision_expiration_status,
    decision_review_cadence_status,
    effective_decision,
    load_analyst_review_decisions,
    queue_decision_state_matches,
)
from med_devices.core.db import connect, init_db
from med_devices.core.fda_mapping_governance import _audit_mapping_rows
from med_devices.core.fda_product_family_review import (
    build_product_family_shadow_score,
    canonical_mdr_family_key,
    load_product_family_exposures,
    load_product_family_mappings,
    mapping_for,
    structured_mdr_metadata,
)
from med_devices.core.fda_states import FDA_REVIEW_KNOWN_STATES, MANUAL_FDA_REVIEW_STATES
from med_devices.core.market_policy import is_adjusted_price_row
from med_devices.core.point_in_time import row_is_effective_asof
from med_devices.core.source_registry import load_source_registry, upsert_source_registry


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REGISTRY = REPO_ROOT / "med_devices" / "data" / "free_source_registry.yaml"
SCRIPT_DIR = REPO_ROOT / "med_devices" / "scripts"
FDA_PRODUCT_FAMILY_MAPPING = REPO_ROOT / "med_devices" / "data" / "fda_product_family_mapping.csv"
FDA_PRODUCT_FAMILY_EXPOSURE = REPO_ROOT / "med_devices" / "data" / "fda_product_family_exposure.csv"
FDA_MANUFACTURER_OVERRIDES = REPO_ROOT / "med_devices" / "data" / "fda_manufacturer_overrides.csv"


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def test_fda_mapping_governance_ignores_zero_reference_ambiguous_dimensions() -> None:
    orphan = {
        "fda_manufacturer_id": "11270",
        "manufacturer_name": "NOVA BIOMEDICAL CORPORATION DIABETES PRODUCTS",
        "mapping_method": "ambiguous",
        "mapping_confidence": "75",
        "total_fda_rows": "0",
    }
    active = {**orphan, "fda_manufacturer_id": "11271", "total_fda_rows": "1"}

    issues, ambiguous_count, high_volume_count, low_confidence_count = _audit_mapping_rows(
        [orphan, active],
        active_companies={},
        min_mapped_confidence=75.0,
        low_confidence_review_threshold=90.0,
    )

    assert ambiguous_count == 1
    assert high_volume_count == 0
    assert low_confidence_count == 0
    assert [issue["fda_manufacturer_id"] for issue in issues] == ["11271"]
    assert issues[0]["issue_type"] == "ambiguous_mapping"


def test_xray_reviewed_manufacturer_overrides_cover_verified_dentsply_entities() -> None:
    with FDA_MANUFACTURER_OVERRIDES.open(newline="", encoding="utf-8-sig") as handle:
        rows = {row["fda_manufacturer_id"]: row for row in csv.DictReader(handle)}

    for manufacturer_id, expected_name in {
        "9252": "DENTSPLY IH INC.",
        "10700": "DENTSPLY SIRONA ORTHODONTICS INC.",
        "11130": "SIRONA DENTAL SYSTEMS GMBH",
    }.items():
        row = rows[manufacturer_id]
        assert row["manufacturer_name"] == expected_name
        assert row["ticker"] == "XRAY"
        assert row["company_id"] == "64"
        assert row["confidence"] == "99"
        assert row["mapping_method"] == "manual_override"
        assert row["valid_from"] == "2026-08-10"
        assert row["reviewed_at"] == "2026-08-10"
        assert "owner/operator 2511302" in row["note"]

    footprint_path = REPO_ROOT / "med_devices" / "data" / "fda_company_footprints.csv"
    with footprint_path.open(newline="", encoding="utf-8-sig") as handle:
        footprint = {row["ticker"]: row for row in csv.DictReader(handle)}["XRAY"]
    codes = set(footprint["product_codes"].split(";"))
    assert codes.issuperset({"NOF", "EGS", "DZE", "NXC", "EJW", "EBC", "EKB", "NDP"})
    assert "NMC" not in codes

    mappings, mapping_issues = load_product_family_mappings(
        FDA_PRODUCT_FAMILY_MAPPING,
        asof=date(2026, 8, 12),
    )
    assert not [issue for issue in mapping_issues if issue.severity == "CRITICAL"]
    xray_mappings = {mapping.product_code: mapping.product_family for mapping in mappings if mapping.ticker == "XRAY"}
    assert xray_mappings["NXC"] == "clear_aligners"
    assert xray_mappings["NDP"] == "dental_implant_accessories"
    assert "NMC" not in xray_mappings

    decisions, issues = load_analyst_review_decisions(
        REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    )
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    decision = effective_decision(
        decisions,
        ticker="XRAY",
        cohort="elective_vision_dental_aesthetic_devices",
        asof=date(2026, 8, 12),
    )
    assert decision is not None
    assert decision.review_category == "all"
    assert decision.decision == "data_fix_needed"
    assert decision.allow_portfolio_candidate_override is False

    completed = effective_decision(
        decisions,
        ticker="XRAY",
        cohort="elective_vision_dental_aesthetic_devices",
        asof=date(2026, 8, 14),
    )
    assert completed is not None
    assert completed.review_category == "all"
    assert completed.decision == "watchlist"
    assert completed.allow_portfolio_candidate_override is False

    feature_module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_xray_completed_footprint_test",
    )
    effective_footprints = feature_module.load_footprint_overrides(
        footprint_path,
        asof=date(2026, 8, 13),
    )
    completed_footprint = effective_footprints["XRAY"]
    assert completed_footprint["review_adjusted_fda_state"] == "manual_fda_footprint_device"
    assert completed_footprint["review_reason"] == ("canonical_product_family_mapping_complete_no_class_i_zero_deaths")
    assert set(completed_footprint["product_codes"].split(";")) == {
        "NOF",
        "EGS",
        "DZE",
        "NXC",
        "EJW",
        "EBC",
        "EKB",
        "NDP",
        "DZC",
        "EFA",
        "EFB",
        "EFT",
        "EIA",
        "EJL",
        "EKS",
        "EKX",
        "KMY",
        "LQY",
        "MQC",
        "MUH",
        "NHA",
    }


def test_xray_refresh_ambiguities_have_governed_non_xray_dispositions() -> None:
    with FDA_MANUFACTURER_OVERRIDES.open(newline="", encoding="utf-8-sig") as handle:
        rows = {row["fda_manufacturer_id"]: row for row in csv.DictReader(handle)}

    nvst = rows["487"]
    assert nvst["manufacturer_name"] == "Dental Imaging Technologies Corporation"
    assert nvst["ticker"] == "NVST"
    assert nvst["company_id"] == "50"
    assert nvst["mapping_method"] == "manual_override"

    for manufacturer_id in {"9325", "10035", "7941", "8453", "7292", "6615", "6665", "6664"}:
        row = rows[manufacturer_id]
        assert row["ticker"] == ""
        assert row["company_id"] == ""
        assert row["mapping_method"] == "out_of_universe"


@pytest.mark.parametrize(
    ("ticker", "cohort", "expected_decision"),
    [
        ("RXST", "elective_vision_dental_aesthetic_devices", "data_fix_needed"),
        ("ISRG", "capital_equipment_procedure_platforms", "watchlist"),
        ("SENS", "home_chronic_care_devices_dme_drug_delivery", "watchlist"),
    ],
)
def test_durable_active_review_decisions_survive_queue_category_drift(
    ticker: str,
    cohort: str,
    expected_decision: str,
) -> None:
    decisions, issues = load_analyst_review_decisions(
        REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    )
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    decision = effective_decision(
        decisions,
        ticker=ticker,
        cohort=cohort,
        asof=date(2026, 8, 12),
    )
    assert decision is not None
    assert decision.review_category == "all"
    assert decision.decision == expected_decision
    assert decision.allow_portfolio_candidate_override is False


@pytest.mark.parametrize(
    ("ticker", "cohort", "expected_decision"),
    [
        ("TFX", "hospital_supplies_surgical_consumables_oem", "watchlist"),
        ("EW", "implantable_interventional_devices_direct_payment", "watchlist"),
        ("FMS", "healthcare_services_cro_lab_services", "reject"),
    ],
)
def test_august_regulatory_reconciliation_supersedes_stale_hard_red_decisions(
    ticker: str,
    cohort: str,
    expected_decision: str,
) -> None:
    decisions, issues = load_analyst_review_decisions(
        REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    )
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    decision = effective_decision(
        decisions,
        ticker=ticker,
        cohort=cohort,
        asof=date(2026, 8, 14),
    )
    assert decision is not None
    assert decision.review_category == "all"
    assert decision.decision == expected_decision
    assert decision.allow_portfolio_candidate_override is False


@pytest.mark.parametrize(
    ("ticker", "cohort", "expected_decision"),
    [
        ("CODX", "diagnostics_clinical_tests", "watchlist"),
        ("RXST", "elective_vision_dental_aesthetic_devices", "watchlist"),
        ("TCMD", "home_chronic_care_devices_dme_drug_delivery", "defer"),
    ],
)
def test_august_22_target_reviews_are_recorded_without_portfolio_overrides(
    ticker: str,
    cohort: str,
    expected_decision: str,
) -> None:
    decisions, issues = load_analyst_review_decisions(
        REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    )
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    decision = effective_decision(
        decisions,
        ticker=ticker,
        cohort=cohort,
        asof=date(2026, 8, 14),
    )
    assert decision is not None
    assert decision.review_category == "all"
    assert decision.decision == expected_decision
    assert decision.allow_portfolio_candidate_override is False


def test_confirmed_fda_manufacturer_closure_overrides_are_governed() -> None:
    with FDA_MANUFACTURER_OVERRIDES.open(newline="", encoding="utf-8-sig") as handle:
        rows = {row["fda_manufacturer_id"]: row for row in csv.DictReader(handle)}

    expected = {
        "10799": ("out_of_universe", "", "", "0", "2019-01-04"),
        "10589": ("out_of_universe", "", "", "0", "2019-01-04"),
        "10590": ("manual_override", "MDT", "6", "95", "2019-01-04"),
        "1145": ("out_of_universe", "", "", "0", "2019-01-04"),
        "1148": ("out_of_universe", "", "", "0", "2019-01-04"),
        "1124": ("manual_override", "MDT", "6", "95", "2020-10-01"),
        "1135": ("out_of_universe", "", "", "0", "2019-01-04"),
        "1139": ("out_of_universe", "", "", "0", "2019-01-04"),
        "10644": ("out_of_universe", "", "", "0", "2019-01-04"),
        "987": ("out_of_universe", "", "", "0", "2019-01-04"),
        "60": ("out_of_universe", "", "", "0", "2019-01-04"),
    }
    for manufacturer_id, values in expected.items():
        row = rows[manufacturer_id]
        assert (
            row["mapping_method"],
            row["ticker"],
            row["company_id"],
            row["confidence"],
            row["valid_from"],
        ) == values
        assert row["reviewed_at"] == "2026-08-11"


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


# The 10 governed FDA product-family shadow columns added to both
# feature_fda_product_risk and med_device_daily_scores. Production databases
# received them via _ensure_table_optional_columns (ALTER TABLE ADD COLUMN on
# pre-existing old-schema tables), not via the fresh CREATE TABLE path.
FDA_PRODUCT_FAMILY_SHADOW_COLUMNS = (
    "fda_event_risk_product_family_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "fda_product_family_shadow_available_flag",
    "fda_product_family_shadow_oos_valid_flag",
    "fda_product_family_adjustment_applied_flag",
    "fda_product_family_exposure_available_count",
    "fda_product_family_exposure_waived_count",
    "fda_product_family_exposure_missing_count",
    "fda_product_family_shadow_status",
    "fda_product_family_shadow_reason",
)
FDA_ADJUDICATION_COLUMNS = (
    "fda_adjudication_applied_flag",
    "fda_adjudicated_event_count_24m",
    "fda_raw_death_count_24m",
    "fda_adjudicated_device_death_count_24m",
    "fda_adjudicated_serious_product_event_count_24m",
    "fda_adjudicated_non_device_death_count_24m",
    "fda_scoring_death_count_24m",
    "fda_scoring_injury_count_24m",
    "fda_scoring_malfunction_count_24m",
    "fda_adjudication_status",
    "fda_adjudication_reviewed_at",
)


def test_reviewed_ldt_clia_footprint_is_known_and_not_a_manual_blocker() -> None:
    state = "reviewed_fda_footprint_ldt_clia"

    assert state in FDA_REVIEW_KNOWN_STATES
    assert state not in MANUAL_FDA_REVIEW_STATES


def test_reviewed_ldt_clia_footprints_are_effective_dated() -> None:
    module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_reviewed_ldt_clia_footprint_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "fda_company_footprints.csv"

    before = module.load_footprint_overrides(path, asof=date(2026, 7, 23))
    effective = module.load_footprint_overrides(path, asof=date(2026, 7, 24))

    assert "BDSX" not in before
    assert effective["BDSX"]["review_adjusted_fda_state"] == "reviewed_fda_footprint_ldt_clia"
    assert effective["BDSX"]["expected_cdrh_records"] == "no"
    assert effective["ADPT"]["review_adjusted_fda_state"] == "manual_fda_footprint_device"
    assert effective["ADPT"]["premarket_numbers"] == "DEN170080;K200009"
    assert effective["ADPT"]["product_codes"] == "QDC"
    assert effective["MDAI"]["premarket_numbers"] == "DEN250028"
    assert effective["MDAI"]["product_codes"] == "SHY"
    assert effective["VCYT"]["footprint_category"] == ("centralized_ldt_clia_with_dormant_legacy_clearance")
    assert effective["VCYT"]["premarket_numbers"] == "K130010"
    assert effective["VCYT"]["product_codes"] == "NYI;NSU"
    assert effective["VCYT"]["expected_cdrh_records"] == "legacy_only"


def test_osur_fda_footprint_correction_is_effective_dated() -> None:
    module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_osur_footprint_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "fda_company_footprints.csv"

    before = module.load_footprint_overrides(path, asof=date(2026, 8, 9))
    effective = module.load_footprint_overrides(path, asof=date(2026, 8, 10))

    assert before["OSUR"]["product_codes"] == "MIB;MZF"
    assert before["OSUR"]["review_adjusted_fda_state"] == "mapping_review_required"
    assert effective["OSUR"]["product_codes"] == "MZO;QID;MZF"
    assert effective["OSUR"]["premarket_numbers"] == "P080027;DEN190025"
    assert effective["OSUR"]["review_adjusted_fda_state"] == "manual_fda_footprint_device"
    assert "MIB" not in effective["OSUR"]["product_codes"].split(";")


def test_codx_fda_footprint_correction_is_effective_dated() -> None:
    module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_codx_footprint_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "fda_company_footprints.csv"

    before = module.load_footprint_overrides(path, asof=date(2026, 8, 13))
    effective = module.load_footprint_overrides(path, asof=date(2026, 8, 14))

    assert before["CODX"]["product_codes"] == "OOI"
    assert before["CODX"]["fei_numbers"] == "3014521998"
    assert effective["CODX"]["product_codes"] == "QJR"
    assert effective["CODX"]["fei_numbers"] == ""
    assert effective["CODX"]["review_adjusted_fda_state"] == "manual_fda_footprint_device"
    assert effective["CODX"]["review_adjusted_fda_state"] not in MANUAL_FDA_REVIEW_STATES
    assert effective["CODX"]["review_reason"] == "verified_active_eua_qjr_no_cleared_510k_platform"
    assert "OUJ" not in effective["CODX"]["product_codes"].split(";")
    assert "QKO" not in effective["CODX"]["product_codes"].split(";")


def test_vnrx_incorrect_cpt_expires_before_structural_lab_routing() -> None:
    reimbursement_module = load_script_module(
        "11_build_med_device_reimbursement_features.py",
        "med_device_vnrx_reimbursement_classification_test",
    )
    classification_path = REPO_ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    override_path = REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"

    before = reimbursement_module.load_company_classifications(
        classification_path,
        asof=date(2026, 7, 23),
    )
    effective = reimbursement_module.load_company_classifications(
        classification_path,
        asof=date(2026, 7, 24),
    )
    with override_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    vnrx_code = next(row for row in rows if row["ticker"] == "VNRX")

    assert "VNRX" not in before
    assert effective["VNRX"].billing_category == ("veterinary_commercial_and_precommercial_human")
    assert effective["VNRX"].payment_rate_status == "veterinary_no_cms"
    assert effective["VNRX"].primary_payment_file == "none"
    assert row_is_effective_asof(vnrx_code, date(2026, 7, 23))
    assert not row_is_effective_asof(vnrx_code, date(2026, 7, 24))


def test_wgs_reimbursement_anchor_transitions_to_exome_genome_code_set() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["ticker"] == "WGS"]

    before = {row["reimbursement_code"] for row in rows if row_is_effective_asof(row, date(2026, 7, 23))}
    effective = {row["reimbursement_code"] for row in rows if row_is_effective_asof(row, date(2026, 7, 24))}

    assert before == {"81425"}
    assert effective == {"81415", "81416", "81425", "81426"}


def test_assay_specific_reimbursement_codes_replace_generic_81479_proxies() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    expected = {
        "BDSX": "0080U",
        "BIAF": "0406U",
        "CSTL": "81529",
        "XGN": "0312U",
    }
    for ticker, code in expected.items():
        current_codes = {
            row["reimbursement_code"]
            for row in rows
            if row["ticker"] == ticker and row_is_effective_asof(row, date(2026, 7, 24))
        }
        assert current_codes == {code}

    gral_codes = {
        row["reimbursement_code"]
        for row in rows
        if row["ticker"] == "GRAL" and row_is_effective_asof(row, date(2026, 7, 24))
    }
    assert not gral_codes


def test_generic_81479_has_no_ticker_agnostic_manual_flat_rate() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_manual_payment_rates.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert not [row for row in rows if row["code"] == "81479" and str(row["payment_rate"] or "").strip()]


def test_updated_lab_reimbursement_classifications_are_effective() -> None:
    module = load_script_module(
        "11_build_med_device_reimbursement_features.py",
        "med_device_updated_lab_reimbursement_classification_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    classifications = module.load_company_classifications(
        path,
        asof=date(2026, 7, 24),
    )

    assert classifications["NEO"].billing_category == "laboratory_services"
    assert classifications["NEO"].payment_rate_status == "large_lab_clfs_array"
    assert classifications["PSNL"].primary_payment_file == "cms_moldx_mrd"
    assert classifications["GRAL"].payment_rate_status == "cash_pay_or_out_of_pocket"
    assert classifications["XGN"].primary_payment_file == "cms_clfs_pla_0312u"


def test_tcmd_ncd_policy_override_is_reviewed_and_point_in_time() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_policy_overrides.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    tcmd = next(row for row in rows if row["ticker"] == "TCMD")
    assert tcmd["policy_id"] == "NCD_280.6"
    assert tcmd["reimbursement_code"] == "E0652"
    assert tcmd["source_id"] == "cms_coverage_api"
    assert not row_is_effective_asof(tcmd, date(2026, 7, 23))
    assert row_is_effective_asof(tcmd, date(2026, 7, 24))


def test_company_risk_event_sync_is_pit_filtered_and_idempotent(tmp_path: Path) -> None:
    module = load_script_module(
        "80_sync_med_device_company_risk_events.py",
        "med_device_company_risk_event_sync_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "company_risk_events.csv"

    assert not [row for row in module.load_rows(path, asof="2026-07-13") if row["ticker"] == "TCMD"]
    rows = [row for row in module.load_rows(path, asof="2026-07-14") if row["ticker"] == "TCMD"]
    assert len(rows) == 1

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, company_name, universe_status, is_active,
                first_seen_at, updated_at
            )
            VALUES (
                1, 'TCMD', 'Tactile Systems Technology, Inc.', 'active', 1,
                '2016-07-28', '2026-07-24'
            )
            """
        )
        module.upsert_event(conn, rows[0])
        module.upsert_event(conn, rows[0])
        stored = conn.execute(
            """
            SELECT ticker, url, event_tags, payload_json
            FROM fact_news_event
            WHERE ticker = 'TCMD'
            """
        ).fetchall()

    assert len(stored) == 1
    assert "false_claims_act" in str(stored[0]["event_tags"])
    payload = json.loads(str(stored[0]["payload_json"]))
    assert payload["event_id"] == "doj_usao_ma_2026_07_14_tcmd_fca"
    assert payload["amount_usd"] == 550959.0


def test_postmarket_event_evidence_distinguishes_confirmed_from_raw_signals() -> None:
    module = load_script_module(
        "80_sync_med_device_company_risk_events.py",
        "med_device_postmarket_evidence_test",
    )
    path = REPO_ROOT / "med_devices" / "data" / "company_risk_events.csv"
    rows = module.load_rows(path, asof="2026-07-24")
    by_id = {row["event_id"]: row for row in rows}

    dxcm = by_id["fda_dxcm_class_i_96743_2025_07_17"]
    assert dxcm["recall_event_id"] == "96743"
    assert dxcm["confirmed_injuries"] == "56"
    assert dxcm["confirmed_deaths"] == "0"
    assert dxcm["raw_mdr_signal_count"] == "129"
    assert dxcm["raw_mdr_signal_status"] == "unverified_raw_mdr_signal"

    rmd_second_family = by_id["fda_rmd_class_i_93122_2023_10_23"]
    assert rmd_second_family["recall_event_id"] == "93122"
    assert rmd_second_family["recall_numbers"] == "Z-0111-2024"
    assert rmd_second_family["remediation_status"] == "open_field_correction"

    rmd_early_alert = by_id["fda_rmd_astral_early_alert_2026_07_15"]
    assert rmd_early_alert["event_type"] == "fda_early_alert"
    assert rmd_early_alert["recall_event_id"] == ""
    assert rmd_early_alert["confirmed_injuries"] == "5"
    assert rmd_early_alert["confirmed_deaths"] == "0"


def test_reimbursement_linker_replaces_stale_links_for_active_taxonomy_company(
    tmp_path: Path,
) -> None:
    module = load_script_module(
        "15_link_med_device_reimbursement_to_companies.py",
        "med_device_reimbursement_linker_company_scope_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(
            conn,
            [
                {
                    "source_id": "cms_payment_files",
                    "stage": "reimbursement",
                    "source_name": "CMS payment files",
                    "source_type": "file",
                    "base_url": "https://www.cms.gov/",
                    "authentication_required": 0,
                    "free_key_required": 0,
                    "priority": 10,
                    "status": "active",
                }
            ],
        )
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, company_name, universe_status, is_active,
                first_seen_at, updated_at
            )
            VALUES (1, 'VNRX', 'VolitionRx Limited', 'active', 1, '2020-01-01', '2026-07-23')
            """
        )
        conn.execute(
            """
            INSERT INTO dim_company_model_taxonomy(
                company_id, model_family, ticker, company_name, calibration_cohort,
                valid_from, reviewed_at, updated_at
            )
            VALUES (
                1, 'med_devices', 'VNRX', 'VolitionRx Limited', 'diagnostics_clinical_tests',
                '2020-01-01', '2026-07-23', '2026-07-23'
            )
            """
        )
        reimbursement_code_id = int(
            conn.execute(
                """
                INSERT INTO dim_reimbursement_code(
                    code_type, code, source_id, created_at, updated_at
                )
                VALUES ('CPT', '87631', 'cms_payment_files', '2026-06-26', '2026-06-26')
                RETURNING reimbursement_code_id
                """
            ).fetchone()["reimbursement_code_id"]
        )
        conn.execute(
            """
            INSERT INTO map_company_reimbursement_code(
                company_id, reimbursement_code_id, confidence, mapping_method,
                source_id, created_at, updated_at
            )
            VALUES (1, ?, 95.0, 'direct_molecular_match', 'cms_payment_files',
                    '2026-06-26', '2026-06-26')
            """,
            (reimbursement_code_id,),
        )
        policy = module.LinkPolicy(
            source_ids=["cms_coverage_api"],
            code_source_ids=["cms_coverage_api", "cms_payment_files"],
            min_auto_confidence=65.0,
            exact_alias_confidence=92.0,
            core_alias_confidence=82.0,
            ticker_confidence=60.0,
            min_term_length=5,
            max_policy_rows=0,
        )

        company_meta = module.load_company_meta(
            conn,
            ticker_filter={"VNRX"},
            asof="2026-07-24",
        )
        aliases = module.build_aliases(
            conn,
            ticker_filter={"VNRX"},
            policy=policy,
            asof="2026-07-24",
        )
        module.clear_existing_mappings(conn, sorted(company_meta), policy=policy)
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM map_company_reimbursement_code WHERE company_id = 1"
        ).fetchone()

    assert company_meta == {1: ("VNRX", "VolitionRx Limited")}
    assert any(alias.company_id == 1 for alias in aliases)
    assert int(remaining["n"]) == 0


def test_july_analyst_decision_replacements_preserve_point_in_time_history() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    expected = {
        "ABT": ("implantable_interventional_devices_direct_payment", "approve", "data_fix_needed"),
        "BSX": ("implantable_interventional_devices_direct_payment", "approve", "watchlist"),
        "GEHC": ("capital_equipment_procedure_platforms", "approve", "watchlist"),
        "ICUI": ("hospital_supplies_surgical_consumables_oem", "approve", "watchlist"),
        "ISRG": ("capital_equipment_procedure_platforms", "approve", "watchlist"),
        "RXST": ("elective_vision_dental_aesthetic_devices", "approve", "data_fix_needed"),
        "TCMD": ("home_chronic_care_devices_dme_drug_delivery", "defer", "defer"),
        "XRAY": ("elective_vision_dental_aesthetic_devices", "approve", "data_fix_needed"),
    }
    for ticker, (cohort, before_value, after_value) in expected.items():
        before = effective_decision(
            decisions,
            ticker=ticker,
            cohort=cohort,
            asof=date(2026, 7, 23),
        )
        after = effective_decision(
            decisions,
            ticker=ticker,
            cohort=cohort,
            asof=date(2026, 7, 24),
        )
        assert before is not None and before.decision == before_value
        assert after is not None and after.decision == after_value


def test_diagnostics_research_review_decisions_are_same_day_exclusive() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    expected = {
        "VCYT": "watchlist",
        "WGS": "watchlist",
        "VNRX": "defer",
    }
    for ticker, decision_value in expected.items():
        same_day = effective_decision(
            decisions,
            ticker=ticker,
            cohort="diagnostics_clinical_tests",
            asof=date(2026, 7, 24),
        )
        effective = effective_decision(
            decisions,
            ticker=ticker,
            cohort="diagnostics_clinical_tests",
            asof=date(2026, 7, 25),
        )

        assert same_day is None
        assert effective is not None
        assert effective.decision == decision_value
        assert effective.allow_portfolio_candidate_override is False


def test_august_data_fix_closures_preserve_pit_history_and_re_gate_as_watchlist() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    cohorts = {
        "ADPT": "diagnostics_clinical_tests",
        "MDAI": "capital_equipment_procedure_platforms",
        "BDSX": "diagnostics_clinical_tests",
        "CSTL": "diagnostics_clinical_tests",
        "NEO": "diagnostics_clinical_tests",
        "PSNL": "diagnostics_clinical_tests",
        "XGN": "diagnostics_clinical_tests",
        "WGS": "diagnostics_clinical_tests",
        "ENOV": "orthopedics_spine_sports_implants",
        "ITGR": "hospital_supplies_surgical_consumables_oem",
        "QDEL": "diagnostics_clinical_tests",
    }
    for ticker, cohort in cohorts.items():
        prior = effective_decision(decisions, ticker=ticker, cohort=cohort, asof=date(2026, 8, 10))
        effective = effective_decision(decisions, ticker=ticker, cohort=cohort, asof=date(2026, 8, 11))

        assert prior is not None and prior.decision == "data_fix_needed"
        assert effective is not None and effective.decision == "watchlist"
        assert effective.review_category == "all"
        assert effective.allow_portfolio_candidate_override is False


def test_decision_lifecycle_rows_exclude_future_and_same_day_reviews() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    historical_rows = decision_lifecycle_rows(
        decisions,
        asof=date(2026, 8, 7),
        warning_days=14,
    )
    effective_rows = decision_lifecycle_rows(
        decisions,
        asof=date(2026, 8, 11),
        warning_days=14,
    )

    assert historical_rows
    assert all(str(row["reviewed_at"]) < "2026-08-07" for row in historical_rows)
    closure_tickers = {
        str(row["ticker"])
        for row in effective_rows
        if row["reviewed_at"] == "2026-08-10" and row["decision"] == "watchlist"
    }
    assert closure_tickers == {
        "ADPT",
        "BDSX",
        "CSTL",
        "ENOV",
        "ITGR",
        "MDAI",
        "NEO",
        "OSUR",
        "PSNL",
        "QDEL",
        "WGS",
        "XGN",
    }


def test_queue_decision_state_matcher_covers_every_governed_lifecycle_state() -> None:
    active = AnalystReviewDecision(
        ticker="RMD",
        calibration_cohort="home_chronic_care_devices_dme_drug_delivery",
        review_category="all",
        decision="watchlist",
        decision_reason="governed review",
        review_owner="portfolio_research",
        reviewed_at="2026-07-24",
        expires_at="",
        next_review_at="2026-08-24",
        active=True,
        allow_portfolio_candidate_override=False,
        max_position_weight_override=None,
        source_reference="test",
        row_number=1,
    )
    expired = AnalystReviewDecision(**{**active.__dict__, "active": False, "expires_at": "2026-08-01"})

    for status in (
        "decided",
        "decision_expires_soon",
        "decision_review_due_soon",
        "decision_review_overdue",
    ):
        assert queue_decision_state_matches(
            status=status,
            recorded_decision="watchlist",
            active_decision=active,
            expired_decision=None,
        )
    assert queue_decision_state_matches(
        status="expired_decision_needs_review",
        recorded_decision="watchlist",
        active_decision=None,
        expired_decision=expired,
    )
    assert queue_decision_state_matches(
        status="open",
        recorded_decision="",
        active_decision=None,
        expired_decision=None,
    )
    assert not queue_decision_state_matches(
        status="open",
        recorded_decision="watchlist",
        active_decision=None,
        expired_decision=None,
    )
    assert not queue_decision_state_matches(
        status="unknown_future_state",
        recorded_decision="",
        active_decision=None,
        expired_decision=None,
    )


def test_dxcm_and_rmd_postmarket_decisions_are_same_day_exclusive_and_scheduled() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    for ticker, expected_decision in {"DXCM": "defer", "RMD": "watchlist"}.items():
        same_day = effective_decision(
            decisions,
            ticker=ticker,
            cohort="home_chronic_care_devices_dme_drug_delivery",
            asof=date(2026, 7, 24),
        )
        effective = effective_decision(
            decisions,
            ticker=ticker,
            cohort="home_chronic_care_devices_dme_drug_delivery",
            asof=date(2026, 7, 25),
        )
        assert same_day is None
        assert effective is not None
        assert effective.decision == expected_decision
        assert effective.review_category == "all"
        assert effective.allow_portfolio_candidate_override is False

    rmd = effective_decision(
        decisions,
        ticker="RMD",
        cohort="home_chronic_care_devices_dme_drug_delivery",
        asof=date(2026, 7, 25),
    )
    assert rmd is not None
    assert rmd.expires_at == ""
    assert rmd.next_review_at == "2026-08-24"
    assert decision_expiration_status(
        rmd,
        asof=date(2026, 7, 25),
        warning_days=7,
    ) == ("active_no_expiration", None, 0)
    assert decision_review_cadence_status(
        rmd,
        asof=date(2026, 8, 18),
        warning_days=7,
    ) == ("review_due_soon", 6, 1)


def test_abt_product_family_governance_is_effective_dated_and_complete() -> None:
    before, before_issues = load_product_family_mappings(
        FDA_PRODUCT_FAMILY_MAPPING,
        asof=date(2026, 7, 23),
    )
    effective, effective_issues = load_product_family_mappings(
        FDA_PRODUCT_FAMILY_MAPPING,
        asof=date(2026, 7, 24),
    )
    exposures, exposure_issues = load_product_family_exposures(
        FDA_PRODUCT_FAMILY_EXPOSURE,
        asof=date(2026, 7, 24),
    )

    assert not before
    assert not [issue for issue in before_issues if issue.severity == "CRITICAL"]
    assert not [issue for issue in effective_issues if issue.severity == "CRITICAL"]
    assert not [issue for issue in exposure_issues if issue.severity == "CRITICAL"]
    assert {mapping.product_code for mapping in effective if mapping.ticker == "ABT"}.issuperset(
        {"DSQ", "QBJ", "QLG", "NGV", "NKM", "OAE", "DQK"}
    )
    governed_families = {exposure.product_family for exposure in exposures if exposure.ticker == "ABT"}
    assert governed_families.issuperset(
        {
            "diabetes_cgm",
            "lvad_circulatory_support",
            "structural_heart",
            "cardiac_rhythm_management",
            "neuromodulation_pain",
            "electrophysiology_ablation",
            "vascular_intervention",
            "diagnostics_laboratory",
            "cardiopulmonary_surgical_support",
        }
    )
    available = {exposure.product_family: exposure for exposure in exposures if exposure.exposure_status == "available"}
    assert len(available) == 8
    assert available["diabetes_cgm"].exposure_value == 7600.0
    assert available["diabetes_cgm"].exposure_scope == "product_family"
    assert available["lvad_circulatory_support"].exposure_scope == ("business_subsegment_fallback")
    assert available["lvad_circulatory_support"].exposure_confidence == 75.0
    waived = {
        exposure.product_family for exposure in exposures if exposure.exposure_status == "waived_no_specific_exposure"
    }
    assert waived == {"cardiopulmonary_surgical_support"}

    closure_mappings, closure_issues = load_product_family_mappings(
        FDA_PRODUCT_FAMILY_MAPPING,
        asof=date(2026, 8, 10),
    )
    assert not [issue for issue in closure_issues if issue.severity == "CRITICAL"]
    mtd = mapping_for(
        closure_mappings,
        ticker="ABT",
        product_code="MTD",
        manufacturer_id=183,
    )
    assert mtd is not None
    assert mtd.product_family == "electrophysiology_ablation"
    assert mtd.mapping_confidence == 99.0


def test_product_family_shadow_uses_governed_waiver_floor() -> None:
    exposures, issues = load_product_family_exposures(
        FDA_PRODUCT_FAMILY_EXPOSURE,
        asof=date(2026, 7, 24),
    )
    assert not [issue for issue in issues if issue.severity == "CRITICAL"]

    shadow = build_product_family_shadow_score(
        ticker="ABT",
        mdr_rows=[
            {
                "product_family": "diabetes_cgm",
                "death_designated_flag": 0,
                "injury_flag": 1,
                "malfunction_flag": 3,
            },
            {
                "product_family": "cardiopulmonary_surgical_support",
                "death_designated_flag": 0,
                "injury_flag": 0,
                "malfunction_flag": 2,
            },
        ],
        recall_rows=[
            {
                "product_family": "cardiopulmonary_surgical_support",
            }
        ],
        exposures=exposures,
        exposure_floor_usd_millions=500.0,
        class_i_recall_severity=5.0,
        recall_severity_rate_weight=4.0,
        class_i_recall_count_weight=20.0,
        death_rate_weight=5.0,
        injury_rate_weight=0.5,
        malfunction_rate_weight=0.1,
    )

    assert shadow.available_flag == 1
    assert shadow.adjustment_applied_flag == 1
    assert shadow.exposure_waived_count == 1
    assert shadow.exposure_missing_count == 0
    assert shadow.status == "ready_with_waiver"
    assert shadow.event_risk_score is not None
    waived_detail = next(
        item for item in shadow.family_details if item["product_family"] == "cardiopulmonary_surgical_support"
    )
    assert waived_detail["denominator_usd_millions"] == 500.0
    assert waived_detail["denominator_source"] == "governed_waiver_conservative_floor"


def test_product_family_shadow_validator_resolves_effective_locked_model_version() -> None:
    module = load_script_module(
        "79_validate_med_device_fda_product_family_shadow.py",
        "med_device_product_family_shadow_validator_version_test",
    )
    config = {
        "scoring": {
            "model_version": "base_v1",
            "ic_tilted_composite": {
                "phase1_safety_lock": True,
                "production_score_regime_effective_from": "2026-07-27",
                "locked_scoring_model_version": "locked_v2",
            },
        }
    }

    assert (
        module.effective_scoring_model_version(
            config,
            asof=date(2026, 7, 26),
        )
        == "base_v1"
    )
    assert (
        module.effective_scoring_model_version(
            config,
            asof=date(2026, 7, 27),
        )
        == "locked_v2"
    )


def test_product_family_mapping_prefers_manufacturer_specific_row(tmp_path: Path) -> None:
    path = tmp_path / "mapping.csv"
    path.write_text(
        "\n".join(
            [
                (
                    "ticker,product_code,fda_manufacturer_id,product_family,"
                    "mapping_confidence,mapping_method,source_reference,valid_from,"
                    "valid_to,reviewed_at,active,notes"
                ),
                "ABT,DSQ,,generic_family,95,manual,test,2026-07-24,,2026-07-24,true,",
                "ABT,DSQ,10680,exact_family,99,manual,test,2026-07-24,,2026-07-24,true,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    mappings, issues = load_product_family_mappings(
        path,
        asof=date(2026, 7, 24),
    )

    assert not [issue for issue in issues if issue.severity == "CRITICAL"]
    exact = mapping_for(
        mappings,
        ticker="ABT",
        product_code="DSQ",
        manufacturer_id=10680,
    )
    generic = mapping_for(
        mappings,
        ticker="ABT",
        product_code="DSQ",
        manufacturer_id=99999,
    )
    assert exact is not None and exact.product_family == "exact_family"
    assert generic is not None and generic.product_family == "generic_family"


def test_structured_mdr_metadata_does_not_infer_from_narrative() -> None:
    metadata = structured_mdr_metadata(
        json.dumps(
            {
                "mdr_report_key": "123",
                "report_number": "ABC-1",
                "summary_report_flag": "Y",
                "noe_summarized": "10",
                "type_of_report": ["Initial submission"],
                "suppl_dates_fda_received": ["2026-07-01"],
                "patient": [{"sequence_number_outcome": ["Hospitalization"]}],
                "mdr_text": [{"text": "The patient died after an unrelated event."}],
            }
        )
    )

    assert metadata["summary_report_flag"] == 1
    assert metadata["number_events_summarized"] == 10
    assert metadata["supplement_count"] == 1
    assert metadata["structured_death_outcome_flag"] == 0


def load_script_module(filename: str, module_name: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_finra_short_interest_parse_asof_rejects_malformed_dates() -> None:
    module = load_script_module("65_update_med_device_finra_short_interest.py", "med_device_finra_parse_asof_test")

    assert module.parse_asof("2026-06-30", field_name="history") == date(2026, 6, 30)
    with pytest.raises(ValueError, match="Invalid history"):
        module.parse_asof("not-a-date", field_name="history")


def test_stage1_schema_creates_independent_med_device_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "med_devices.sqlite"

    with connect(db_path) as conn:
        init_db(conn)
        names = table_names(conn)
        price_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(fact_price_ohlcv)").fetchall()}
        fda_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(feature_fda_product_risk)").fetchall()
        }
        score_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(med_device_daily_scores)").fetchall()
        }

    assert "source_registry" in names
    assert "raw_api_responses" in names
    assert "dim_company" in names
    assert "fact_fda_approval" in names
    assert "fact_reimbursement_policy" in names
    assert "med_device_daily_scores" in names
    assert "daily_scores" not in names
    assert "trials" not in names
    assert "price_adjustment" in price_columns
    assert "fda_event_risk_product_family_adjusted_score" in fda_columns
    assert "fda_safety_product_family_adjusted_score" in fda_columns
    assert "fda_product_family_shadow_status" in fda_columns
    assert "fda_product_family_shadow_oos_valid_flag" in fda_columns
    assert set(FDA_ADJUDICATION_COLUMNS).issubset(fda_columns)
    assert "fda_event_risk_product_family_adjusted_score" in score_columns
    assert "fda_safety_product_family_adjusted_score" in score_columns
    assert "fda_product_family_shadow_oos_valid_flag" in score_columns
    assert set(FDA_ADJUDICATION_COLUMNS).issubset(score_columns)


def test_stage1_schema_migrates_shadow_columns_onto_pre_migration_tables(tmp_path: Path) -> None:
    # SC-3: the fresh CREATE TABLE path above never exercises the ALTER-based
    # migration the live DB actually used (_ensure_table_optional_columns on an
    # existing old-schema table). Simulate that path by dropping the 10 shadow
    # columns from a fully-initialized DB and re-running init_db: CREATE TABLE
    # IF NOT EXISTS is a no-op on existing tables, so only the migration dicts
    # can restore them. A future edit that adds a shadow column to the CREATE
    # TABLE text but omits the migration dict entry fails here instead of on
    # the first insert against a migrated production DB.
    assert sqlite3.sqlite_version_info >= (3, 35, 0), (
        "ALTER TABLE DROP COLUMN requires SQLite >= 3.35.0; the migration-path "
        f"test cannot run on sqlite {sqlite3.sqlite_version}"
    )
    db_path = tmp_path / "med_devices.sqlite"
    tables = ("feature_fda_product_risk", "med_device_daily_scores")
    with connect(db_path) as conn:
        init_db(conn)
        fresh_columns = {table: table_columns(conn, table) for table in tables}
        for table in tables:
            missing_from_create = [
                column for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS if column not in fresh_columns[table]
            ]
            assert not missing_from_create, f"{table} CREATE TABLE is missing shadow columns: {missing_from_create}"
            for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            pre_migration = table_columns(conn, table)
            assert not pre_migration.intersection(FDA_PRODUCT_FAMILY_SHADOW_COLUMNS)

        init_db(conn)

        for table in tables:
            migrated = table_columns(conn, table)
            not_migrated = [column for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS if column not in migrated]
            assert not not_migrated, (
                f"init_db did not migrate shadow columns onto pre-migration {table} "
                f"(missing from _ensure_table_optional_columns dict?): {not_migrated}"
            )
            # CREATE-vs-migration parity: a migrated old-schema table must end
            # up with exactly the same column surface as a fresh CREATE.
            assert migrated == fresh_columns[table], (
                f"{table} migrated column set diverges from fresh CREATE TABLE: "
                f"only_in_create={sorted(fresh_columns[table] - migrated)} "
                f"only_in_migrated={sorted(migrated - fresh_columns[table])}"
            )


def test_free_source_registry_loads_core_free_sources(tmp_path: Path) -> None:
    sources = load_source_registry(SOURCE_REGISTRY)
    source_ids = {str(row["source_id"]) for row in sources}

    assert {
        "sec_company_tickers",
        "sec_companyfacts",
        "ib_market_data",
        "yahoo_finance_backup",
        "openfda_device",
        "accessgudid",
        "cms_coverage_api",
        "clinicaltrials_v2",
        "fred",
    }.issubset(source_ids)
    assert "stooq_daily_prices" not in source_ids
    assert "alpha_vantage_daily_prices" not in source_ids

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        count = upsert_source_registry(conn, sources)
        row = conn.execute("SELECT COUNT(*) AS source_count FROM source_registry").fetchone()

    assert count == len(sources)
    assert row is not None
    assert int(row["source_count"]) == len(sources)


def test_source_registry_preserves_zero_priority(tmp_path: Path) -> None:
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "\n".join(
            [
                "sources:",
                "  - source_id: first_source",
                "    stage: stage_1",
                "    source_name: First",
                "    source_type: api",
                "    base_url: https://example.com/first",
                "    priority: 0",
                "  - source_id: second_source",
                "    stage: stage_1",
                "    source_name: Second",
                "    source_type: api",
                "    base_url: https://example.com/second",
                "    priority: 10",
            ]
        ),
        encoding="utf-8",
    )

    sources = load_source_registry(registry)

    assert [source["source_id"] for source in sources] == ["first_source", "second_source"]
    assert sources[0]["priority"] == 0


def test_market_policy_treats_zero_adjusted_close_as_present() -> None:
    assert is_adjusted_price_row({"is_adjusted": 1, "adj_close": 0.0})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": 0.0})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": "nan"})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": None})


def test_med_device_universe_loader_accepts_clean_keep_shape(tmp_path: Path) -> None:
    module = load_script_module("01_load_med_device_universe.py", "med_device_universe_loader_test")
    universe_csv = tmp_path / "med_dev_tickers_clean_keep.csv"
    universe_csv.write_text(
        "\n".join(
            [
                "Name,Company_Name,Industry,Index,CIK,Exchange,SecurityType,ListingStatus,IsPrimaryListing,Country,Currency,CompanyName,MatchedTicker,MatchType,Source,IdentityDataSources,MissingIdentityFields,ManualInclude,ManualExclude,ManualReview,Notes",
                'ISRG,"Intuitive Surgical, Inc.",Healthcare,Medical Instruments & Supplies,0001035267,Nasdaq,Common Stock,active,TRUE,United States,USD,INTUITIVE SURGICAL INC,ISRG,exact,sec,nasdaqtrader,,false,false,false,',
                "MDT,Medtronic plc,Healthcare,Medical Devices,0001613103,NYSE,Ordinary Shares,active,TRUE,United States,USD,Medtronic plc,MDT,exact,sec,nasdaqtrader,,,,,",
            ]
        ),
        encoding="utf-8",
    )
    companies = module.parse_universe_rows(universe_csv)
    assert [company.ticker for company in companies] == ["ISRG", "MDT"]
    assert companies[0].cik == "0001035267"
    assert companies[0].subsector == "medical_instruments_and_supplies"
    assert companies[0].medtech_pure_play_flag == 1

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        module.upsert_universe(conn, companies)
        row = conn.execute("SELECT COUNT(*) AS company_count FROM dim_company").fetchone()
        security_row = conn.execute(
            "SELECT security_type FROM dim_security WHERE ticker = ?",
            ("MDT",),
        ).fetchone()
        company_row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", ("ISRG",)).fetchone()

    assert row is not None
    assert int(row["company_count"]) == 2
    assert security_row is not None
    assert security_row["security_type"] == "Ordinary Shares"
    assert company_row is not None


def test_med_device_universe_loader_keeps_otc_issuer_active_but_non_investable(
    tmp_path: Path,
) -> None:
    module = load_script_module("01_load_med_device_universe.py", "med_device_otc_universe_test")
    universe_csv = tmp_path / "med_dev_tickers_clean_keep.csv"
    universe_csv.write_text(
        "\n".join(
            [
                "ticker,investability_status,company_name,cik,exchange,sector,industry,medtech_subsector,country,currency,security_type,listing_status,is_primary_listing",
                'GCTK,investable,"GlucoTrack, Inc.",0001506983,Nasdaq,Healthcare,Healthcare,medical_instruments_and_supplies,United States,USD,Common Stock,active,1',
            ]
        ),
        encoding="utf-8",
    )

    company = module.parse_universe_rows(
        universe_csv,
        config={
            "universe_validation": {
                "non_investable_exchanges": ["otcqb"],
                "ticker_listing_overrides": {
                    "GCTK": {
                        "exchange": "OTCQB",
                        "listing_status": "active",
                        "reviewed_at": "2026-08-15",
                        "reason": "former_nasdaq_listing_delisted",
                    }
                },
            }
        },
    )[0]

    assert company.exchange == "OTCQB"
    assert company.investability_status == "non_investable_exchange"
    assert company.universe_status == "active_non_investable_otc"
    assert company.is_active == 1

    prior = replace(
        company,
        exchange="Nasdaq",
        investability_status="investable",
        universe_status="keep",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        module.upsert_universe(conn, [prior])
        module.upsert_universe(conn, [company])
        securities = conn.execute(
            """
            SELECT exchange, listing_status, is_primary_listing
            FROM dim_security WHERE ticker = 'GCTK' ORDER BY exchange
            """
        ).fetchall()

    by_exchange = {row["exchange"]: row for row in securities}
    assert by_exchange["Nasdaq"]["listing_status"] == "delisted"
    assert by_exchange["Nasdaq"]["is_primary_listing"] == 0
    assert by_exchange["OTCQB"]["listing_status"] == "active"
    assert by_exchange["OTCQB"]["is_primary_listing"] == 1


@pytest.mark.parametrize(
    ("ticker", "expected_before", "expected_after"),
    [
        ("TMDX", "reject", "approve"),
        ("PRCT", "reject", "watchlist"),
        ("OWLT", "watchlist", "reject"),
    ],
)
def test_august_15_capital_platform_governance_decisions_preserve_pit_history(
    ticker: str,
    expected_before: str,
    expected_after: str,
) -> None:
    decisions, issues = load_analyst_review_decisions(
        REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    )
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]

    before = effective_decision(
        decisions,
        ticker=ticker,
        cohort="capital_equipment_procedure_platforms",
        asof=date(2026, 8, 14),
    )
    after = effective_decision(
        decisions,
        ticker=ticker,
        cohort="capital_equipment_procedure_platforms",
        asof=date(2026, 8, 17),
    )

    assert before is not None and before.decision == expected_before
    assert after is not None and after.decision == expected_after
    assert after.allow_portfolio_candidate_override is False


def test_yahoo_adjusted_parser_builds_adjusted_price_rows() -> None:
    module = load_script_module("04_sync_med_device_yahoo_adjusted_prices.py", "med_device_yahoo_sync_test")
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1704067200, 1704153600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, 20.0],
                                "high": [11.0, 22.0],
                                "low": [9.0, 18.0],
                                "close": [10.0, 20.0],
                                "volume": [1000, 2000],
                            }
                        ],
                        "adjclose": [{"adjclose": [5.0, 20.0]}],
                    },
                    "events": {
                        "dividends": {"1704153600": {"amount": 0.12}},
                        "splits": {"1704067200": {"numerator": 2.0, "denominator": 1.0}},
                    },
                }
            ]
        }
    }

    bars = module.parse_bars("AAA", payload, source_id="yahoo_finance_backup")

    assert len(bars) == 2
    assert bars[0].ticker == "AAA"
    assert bars[0].close == 10.0
    assert bars[0].adj_close == 5.0
    assert bars[0].open == 10.0
    assert bars[0].price_adjustment == "adjusted"
    assert bars[0].is_adjusted == 1
    assert bars[0].split_factor == 2.0
    assert bars[1].dividend_amount == 0.12


def test_sec_ingestion_parses_filings_and_companyfacts() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_sync_test")
    company = module.Company(company_id=1, ticker="AAA", cik="0000000001", company_name="AAA Medical")
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002"],
                "form": ["10-K", "8-K"],
                "filingDate": ["2026-02-15", "2026-03-01"],
                "reportDate": ["2025-12-31", "2026-02-28"],
                "primaryDocument": ["aaa-20251231.htm", "aaa-8k.htm"],
            }
        }
    }
    filings = module.parse_recent_filings(company, submissions, {"10-K", "8-K"})
    assert len(filings) == 2
    assert filings[0]["accession_nodash"] == "000000000126000001"
    assert filings[0]["source_id"] == "sec_submissions"

    companyfacts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 1000,
                            }
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 120,
                            }
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 20,
                            }
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 300,
                            }
                        ]
                    }
                },
            }
        }
    }
    rows = module.build_financial_statement_rows(company, companyfacts)
    assert len(rows) == 1
    assert rows[0]["period_end"] == "2025-12-31"
    assert rows[0]["revenue"] == 1000
    assert rows[0]["free_cash_flow"] == 100
    assert rows[0]["cash_and_investments"] == 300


def test_sec_inline_xbrl_fallback_is_current_period_and_dimension_free() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_inline_test")
    company = module.Company(company_id=1, ticker="AAA", cik="0000000001", company_name="AAA Medical")
    filing = {
        "accession_nodash": "000000000126000003",
        "form": "10-Q",
        "filing_date": "2026-07-28",
        "report_date": "2026-06-30",
        "primary_document": "aaa-20260630.htm",
        "archive_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000003/aaa-20260630.htm",
    }
    document = """
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance">
      <ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="current">Q2</ix:nonNumeric>
      <ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="current">2026</ix:nonNumeric>
      <xbrli:context id="current"><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="prior"><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="segment"><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period><xbrli:scenario><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">aaa:Segment</xbrldi:explicitMember></xbrli:scenario></xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="current" unitRef="USD">1,200</ix:nonFraction>
      <ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="prior" unitRef="USD">900</ix:nonFraction>
      <ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="segment" unitRef="USD">999</ix:nonFraction>
      <ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" contextRef="current" unitRef="USD">120</ix:nonFraction>
      <ix:nonFraction name="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment" contextRef="current" unitRef="USD">20</ix:nonFraction>
    </html>
    """
    policy = module.sec_ingestion_policy({})
    rows = module.build_inline_fallback_rows(company, filing, document, policy)

    assert len(rows) == 1
    assert rows[0]["period_end"] == "2026-06-30"
    assert rows[0]["fiscal_period"] == "Q2"
    assert rows[0]["revenue"] == 1_200
    assert rows[0]["free_cash_flow"] == 100
    assert rows[0]["source_id"] == "sec_inline_xbrl_filing"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["_filing_fallback"]["comparative_contexts_excluded"] is True


def test_sec_inline_xbrl_fallback_only_targets_unrepresented_filings() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_gap_test")
    policy = module.sec_ingestion_policy({})
    filing = {
        "accession_nodash": "000000000126000003",
        "form": "10-Q",
        "filing_date": "2026-07-28",
        "report_date": "2026-06-30",
        "primary_document": "aaa-20260630.htm",
    }
    prior_rows = [
        {
            "accession_nodash": "000000000126000001",
            "period_end": "2026-03-31",
            "form": "10-Q",
            "filed_date": "2026-04-28",
        }
    ]
    assert module.unrepresented_financial_filings([filing], prior_rows, policy) == [filing]

    represented = [
        {
            "accession_nodash": filing["accession_nodash"],
            "period_end": filing["report_date"],
            "form": filing["form"],
            "filed_date": filing["filing_date"],
        }
    ]
    assert module.unrepresented_financial_filings([filing], represented, policy) == []


def test_avns_delisting_is_effective_dated_and_non_investable() -> None:
    universe_path = REPO_ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv"
    with universe_path.open("r", encoding="utf-8-sig", newline="") as handle:
        universe = {row["ticker"]: row for row in csv.DictReader(handle)}
    assert universe["AVNS"]["listing_status"] == "delisted"
    assert universe["AVNS"]["is_primary_listing"] == "0"

    membership_path = REPO_ROOT / "med_devices" / "data" / "med_device_historical_membership.csv"
    with membership_path.open("r", encoding="utf-8-sig", newline="") as handle:
        membership = {row["internal_ticker"]: row for row in csv.DictReader(handle)}
    assert membership["AVNS"]["start_date"] == "2014-10-21"
    assert membership["AVNS"]["end_date"] == "2026-07-24"
    assert membership["AVNS"]["membership_status"] == "historical"
    assert membership["AVNS"]["event_type"] == "acquired_private"

    config_text = (REPO_ROOT / "med_devices" / "config.yaml").read_text(encoding="utf-8")
    assert "non_investable_listing_statuses:\n" in config_text
    assert "    - delisted\n" in config_text
    source_ids = {
        row["source_id"] for row in load_source_registry(REPO_ROOT / "med_devices/data/free_source_registry.yaml")
    }
    assert "sec_inline_xbrl_filing" in source_ids


def test_sec_ingestion_derives_reviewed_gross_profit_with_provenance() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_gross_profit_test")
    company = module.Company(company_id=95, ticker="CERS", cik="0001020214", company_name="Cerus Corporation")

    def duration_fact(value: float) -> dict[str, object]:
        return {
            "units": {
                "USD": [
                    {
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "fy": 2025,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-03-02",
                        "accn": "0001193125-26-085678",
                        "val": value,
                    }
                ]
            }
        }

    companyfacts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": duration_fact(1_000.0),
                "OperatingIncomeLoss": duration_fact(-50.0),
                "SellingGeneralAndAdministrativeExpense": duration_fact(350.0),
                "ResearchAndDevelopmentExpense": duration_fact(200.0),
            }
        }
    }
    default_rows = module.build_financial_statement_rows(company, companyfacts)
    assert default_rows[0]["gross_profit"] is None

    policy = module.sec_ingestion_policy(
        {"sec_ingestion": {"annual_gross_profit_from_operating_expenses_tickers": ["cers"]}}
    )
    rows = module.build_financial_statement_rows(company, companyfacts, policy)
    assert rows[0]["gross_profit"] == pytest.approx(500.0)
    payload = json.loads(rows[0]["payload_json"])
    assert payload["gross_profit"]["derived"] is True
    assert payload["gross_profit"]["concept"] == "derived_operating_income_plus_sga_and_rd"
    assert payload["gross_profit"]["inputs"]["revenue"] == 1_000.0

    companyfacts["facts"]["us-gaap"]["OperatingIncomeLoss"] = duration_fact(600.0)
    invalid_rows = module.build_financial_statement_rows(company, companyfacts, policy)
    assert invalid_rows[0]["gross_profit"] is None


def test_cers_regulatory_and_reimbursement_source_contract() -> None:
    fda_path = REPO_ROOT / "med_devices" / "data" / "fda_company_footprints.csv"
    with fda_path.open("r", encoding="utf-8-sig", newline="") as handle:
        fda_rows = {row["ticker"]: row for row in csv.DictReader(handle)}
    assert fda_rows["CERS"]["product_codes"] == "PJF"
    assert fda_rows["CERS"]["premarket_numbers"] == "BP140143"
    assert fda_rows["CERS"]["fei_numbers"] == "3003948751"
    assert fda_rows["CERS"]["review_adjusted_fda_state"] == "regulatory_review_required"
    assert fda_rows["CERS"]["review_reason"] == "structured_postmarket_event_attribution_required"

    evidence_path = REPO_ROOT / "med_devices" / "data" / "fda_manual_footprint_evidence.csv"
    with evidence_path.open("r", encoding="utf-8-sig", newline="") as handle:
        evidence_rows = {row["ticker"]: row for row in csv.DictReader(handle)}
    assert evidence_rows["CERS"]["fda_evidence_type"] == "cber_pma_and_postmarket_mapped"
    assert evidence_rows["CERS"]["regulatory_stage"] == "commercial_pma_postmarket_active"
    assert evidence_rows["CERS"]["source"] == "fda_accessdata_cber_and_openfda_device"

    classification_path = REPO_ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    with classification_path.open("r", encoding="utf-8-sig", newline="") as handle:
        classifications = {row["ticker"]: row for row in csv.DictReader(handle)}
    cers_classification = classifications["CERS"]
    assert cers_classification["billing_category"] == "blood_processing_products"
    assert cers_classification["payment_rate_status"] == "direct_hcpcs_and_bundled_hospital"

    mapping_path = REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as handle:
        cers_codes = {
            row["reimbursement_code"]
            for row in csv.DictReader(handle)
            if row["ticker"] == "CERS" and row["active"] == "1"
        }
    assert cers_codes == {"P9026", "P9070", "P9071", "P9073"}

    reimbursement_module = load_script_module(
        "11_build_med_device_reimbursement_features.py",
        "med_device_cers_reimbursement_status_test",
    )
    assert "direct_hcpcs_and_bundled_hospital" in reimbursement_module.RECOGNIZED_BUNDLED_PAYMENT_STATUSES
    assert "direct_hcpcs_and_bundled_hospital" in reimbursement_module.PROCEDURE_INDIRECT_PAYMENT_STATUSES

    decision_path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(decision_path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    prior = effective_decision(
        decisions,
        ticker="CERS",
        cohort="hospital_supplies_surgical_consumables_oem",
        asof=date(2026, 8, 10),
    )
    effective = effective_decision(
        decisions,
        ticker="CERS",
        cohort="hospital_supplies_surgical_consumables_oem",
        asof=date(2026, 8, 11),
    )
    assert prior is not None and prior.decision == "data_fix_needed"
    assert effective is not None and effective.decision == "defer"
    assert effective.review_category == "all"
    assert effective.allow_portfolio_candidate_override is False


def test_cers_adverse_event_adjudication_is_effective_dated_and_preserves_raw_counts(
    tmp_path: Path,
) -> None:
    module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_cers_event_adjudication_test",
    )
    publishing_module = load_script_module(
        "16_publish_med_device_score_review_pack.py",
        "med_device_cers_event_adjudication_publish_test",
    )
    assert set(FDA_ADJUDICATION_COLUMNS).issubset(publishing_module.SCORE_FIELDS)
    path = REPO_ROOT / "med_devices" / "data" / "fda_adverse_event_adjudications.csv"
    before = module.load_adverse_event_adjudications(
        path,
        asof=date(2026, 8, 9),
    )
    effective = module.load_adverse_event_adjudications(
        path,
        asof=date(2026, 8, 10),
    )
    assert before == {}
    assert len(effective) == 4

    raw_events = [
        ("20993629", "3003925919-2024-00001", "2024-12-20", 0, 1, 0),
        ("21121055", "3003925919-2024-00002", "2025-01-09", 1, 0, 0),
        ("22028783", "3003925919-2025-00001", "2025-05-15", 1, 0, 0),
        ("22038210", "3003925919-2025-00002", "2025-05-16", 1, 0, 0),
    ]
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector,
                country, currency, universe_status, is_active, first_seen_at,
                updated_at
            )
            VALUES (
                1, 'CERS', '0001020214', 'Cerus Corporation', 'NASDAQ',
                'blood_processing', 'United States', 'USD', 'active', 1,
                '2024-01-01', '2026-08-10'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dim_fda_product_code(
                product_code, device_name, medical_specialty, device_class,
                created_at, updated_at
            )
            VALUES (
                'PJF', 'Illuminator System For Blood Products',
                'Hematology', '3', '2026-08-10', '2026-08-10'
            )
            """
        )
        for (
            event_id,
            report_number,
            report_date,
            death_count,
            injury_count,
            malfunction_count,
        ) in raw_events:
            conn.execute(
                """
                INSERT INTO fact_fda_adverse_event(
                    adverse_event_id, company_id, product_code, report_date,
                    death_count, injury_count, malfunction_count, event_type,
                    payload_json, created_at, updated_at
                )
                VALUES (?, 1, 'PJF', ?, ?, ?, ?, ?, ?, '2026-08-10', '2026-08-10')
                """,
                (
                    event_id,
                    report_date,
                    death_count,
                    injury_count,
                    malfunction_count,
                    "Death" if death_count else "Injury",
                    json.dumps(
                        {
                            "mdr_report_key": event_id,
                            "report_number": report_number,
                        }
                    ),
                ),
            )

        policy = module.fda_feature_policy(module.load_yaml(REPO_ROOT / "med_devices" / "config.yaml"))
        raw_row = module.FdaFeatureRow(
            asof_date="2026-08-10",
            company_id=1,
            ticker="CERS",
            company_name="Cerus Corporation",
            revenue_ttm=473_580_000.0,
        )
        module.count_adverse_events(
            conn,
            raw_row,
            asof=date(2026, 8, 10),
            policy=policy,
        )
        module.score_row(raw_row, policy=policy)

        adjudicated_row = module.FdaFeatureRow(
            asof_date="2026-08-10",
            company_id=1,
            ticker="CERS",
            company_name="Cerus Corporation",
            revenue_ttm=473_580_000.0,
        )
        module.count_adverse_events(
            conn,
            adjudicated_row,
            asof=date(2026, 8, 10),
            policy=policy,
            adjudications=effective,
        )
        module.score_row(adjudicated_row, policy=policy)

    assert raw_row.death_count_24m == 3
    assert raw_row.hard_red_flag == 1
    assert adjudicated_row.death_count_24m == 3
    assert adjudicated_row.fda_raw_death_count_24m == 3
    assert adjudicated_row.fda_adjudicated_event_count_24m == 4
    assert adjudicated_row.fda_adjudicated_device_death_count_24m == 0
    assert adjudicated_row.fda_adjudicated_serious_product_event_count_24m == 2
    assert adjudicated_row.fda_adjudicated_non_device_death_count_24m == 2
    assert adjudicated_row.fda_scoring_death_count_24m == 0
    assert adjudicated_row.fda_scoring_injury_count_24m == 2
    assert adjudicated_row.hard_red_flag == 0
    assert adjudicated_row.fda_adjudication_applied_flag == 1
    assert adjudicated_row.fda_adjudication_status == ("effective_event_level_adjudication")
    payload = adjudicated_row.payload or {}
    assert len(payload["adverse_event_adjudication"]["events"]) == 4


def test_sec_metric_sort_ignores_malformed_filed_dates() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_sort_test")
    valid = module.FactObservation(
        metric="revenue",
        concept="Revenue",
        unit="USD",
        value=100.0,
        period_start="2025-01-01",
        period_end="2025-12-31",
        fiscal_year=2025,
        fiscal_period="FY",
        form="10-K",
        filed_date="2026-02-15",
        accession_nodash="valid",
        frame="",
        concept_rank=0,
    )
    malformed = module.FactObservation(
        metric="revenue",
        concept="Revenue",
        unit="USD",
        value=200.0,
        period_start="2025-01-01",
        period_end="2025-12-31",
        fiscal_year=2025,
        fiscal_period="FY",
        form="10-K",
        filed_date="not-a-date",
        accession_nodash="malformed",
        frame="",
        concept_rank=0,
    )

    assert module.sortable_filed_date("not-a-date") == ""
    assert module.observation_sort_key(valid) > module.observation_sort_key(malformed)


def test_financial_feature_builder_computes_ttm_and_valuation(tmp_path: Path) -> None:
    module = load_script_module("06_build_med_device_financial_features.py", "med_device_financial_features_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, created_at, updated_at
            )
            VALUES
                ('yahoo_finance_backup', 'stage_1', 'Yahoo Finance', 'api', 'https://query1.finance.yahoo.com', '2026-01-01', '2026-01-01'),
                ('sec_companyfacts', 'stage_1', 'SEC companyfacts', 'api', 'https://data.sec.gov', '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'AAA', '0000000001', 'AAA Medical', 'NYSE', 'medical_devices',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_price_ohlcv(
                ticker, bar_date, source_id, open, high, low, close, adj_close, volume,
                price_adjustment, is_adjusted, created_at, updated_at
            )
            VALUES ('AAA', '2026-05-22', 'yahoo_finance_backup', 50, 50, 50, 50, 50,
                    1000000, 'adjusted', 1, '2026-05-22', '2026-05-22')
            """
        )
        rows = [
            ("2025-03-31", 2025, "Q1", "10-Q", "2025-04-25", 250, 150, 45, 30, 45, 8, 37, 80, 190, 100),
            ("2024-12-31", 2024, "FY", "10-K", "2025-02-01", 800, 480, 120, 90, 150, 35, 115, 90, 180, 100),
            ("2026-03-31", 2026, "Q1", "10-Q", "2026-04-25", 300, 180, 60, 45, 50, 10, 40, 100, 200, 100),
            ("2025-12-31", 2025, "FY", "10-K", "2026-02-01", 1000, 600, 200, 150, 180, 40, 140, 95, 210, 100),
        ]
        conn.executemany(
            """
            INSERT INTO fact_financial_statement(
                company_id, period_end, fiscal_year, fiscal_period, form, filed_date,
                revenue, gross_profit, operating_income, net_income, operating_cash_flow,
                capital_expenditures, free_cash_flow, cash_and_investments, total_debt,
                shares_outstanding, source_id, created_at, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts', '2026-01-01', '2026-01-01')
            """,
            rows,
        )
        companies = [
            module.Company(company_id=1, ticker="AAA", company_name="AAA Medical", subsector="medical_devices")
        ]
        policy = module.FinancialFeaturePolicy(
            market_sources=["yahoo_finance_backup"],
            share_count_sources=["yahoo_finance_backup", "sec_companyfacts"],
            share_count_max_staleness_days=30,
            allow_sec_weighted_average_share_fallback=True,
            max_staleness_days=7,
            require_adjusted=True,
            core_min_years=1.0,
            core_min_group_years=1.0,
            short_min_years=0.5,
            neutral_component_score=module.DEFAULT_NEUTRAL_COMPONENT_SCORE,
            fundamental_weights=module.DEFAULT_FUNDAMENTAL_COMPONENT_WEIGHTS,
            valuation_weights=module.DEFAULT_VALUATION_COMPONENT_WEIGHTS,
            subsector_blend_weight=0.60,
            winsor_low_pct=0.05,
            winsor_high_pct=0.95,
            ttm_sanity_min_annual_ratio=0.20,
            ttm_sanity_max_annual_ratio=3.00,
        )
        feature_rows = module.build_features(
            conn,
            companies,
            asof=module.parse_date("2026-05-22"),
            policy=policy,
        )
        assert len(feature_rows) == 1
        feature = feature_rows[0]
        assert feature.revenue_ttm == 1050
        assert feature.free_cash_flow_ttm == 143
        assert feature.market_cap == 5000
        assert feature.enterprise_value == 5100
        assert round(feature.ev_to_sales or 0, 4) == round(5100 / 1050, 4)
        assert feature.data_quality_status == "pass"
        assert feature.fundamental_quality_score_v1 is not None
        assert feature.valuation_score_v1 is not None

        module.upsert_feature_rows(conn, feature_rows)
        detail_row = conn.execute("SELECT revenue_ttm FROM feature_financial_valuation WHERE ticker = 'AAA'").fetchone()
        quality_row = conn.execute("SELECT score FROM feature_fundamental_quality WHERE company_id = 1").fetchone()

    assert detail_row is not None
    assert detail_row["revenue_ttm"] == 1050
    assert quality_row is not None
    assert quality_row["score"] is not None


def test_financial_read_csv_flexible_reports_decode_failure(tmp_path: Path) -> None:
    module = load_script_module("06_build_med_device_financial_features.py", "med_device_financial_csv_test")
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_bytes(b"ticker,shares\nAAA,\x81\n")

    try:
        module.read_csv_flexible(bad_csv)
    except ValueError as exc:
        assert "Could not decode CSV" in str(exc)
    else:
        raise AssertionError("Expected read_csv_flexible to raise ValueError for undecodable CSV")


def test_fda_targeted_footprints_cover_cber_and_postmarket_channels(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_targeted_cber_test")
    link_module = load_script_module("09_link_med_device_fda_to_companies.py", "med_device_fda_cber_link_test")
    footprint_csv = tmp_path / "footprints.csv"
    with footprint_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "footprint_category",
                "primary_fda_entity",
                "product_codes",
                "premarket_numbers",
                "expected_cdrh_records",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "CERS",
                "footprint_category": "direct_regulated_device",
                "primary_fda_entity": "Cerus Corporation",
                "product_codes": "PJF",
                "premarket_numbers": "BP140143",
                "expected_cdrh_records": "yes",
            }
        )

    endpoints = module.build_targeted_footprint_endpoints(
        footprint_csv,
        target_limit=1000,
        include_entity_names=True,
        include_postmarket=True,
        tickers={"CERS"},
    )
    by_name = {endpoint.name: endpoint for endpoint in endpoints}

    assert module.is_pma_identifier("BP140143")
    assert module.is_pma_identifier("P160055")
    assert not module.is_pma_identifier("K260001")
    assert by_name["target_pma_BP140143"].search == 'pma_number:"BP140143"'
    assert by_name["target_recall_code_CERS_PJF"].search == (
        'product_code:"PJF" AND recalling_firm:"Cerus Corporation"'
    )
    assert by_name["target_enforcement_code_CERS_PJF"].path == "enforcement.json"
    assert by_name["target_event_code_CERS_PJF"].search == (
        'device.device_report_product_code:"PJF" AND device.manufacturer_d_name:"Cerus Corporation"'
    )
    assert "target_entity_pma_CERS_CERUS_CORPORATION" in by_name
    assert "target_event_entity_CERS_CERUS_CORPORATION" in by_name
    assert len(endpoints) == 9
    assert (
        module.build_targeted_footprint_endpoints(
            footprint_csv,
            target_limit=1000,
            include_postmarket=True,
            tickers={"OTHER"},
        )
        == []
    )
    assert link_module.approval_submission_clause("BP140143") == (
        "(submission_number = ? OR submission_number LIKE ?)",
        ["BP140143", "BP140143-%"],
    )


def test_fda_targeted_footprints_support_denovo_and_supersede_old_product_codes(
    tmp_path: Path,
) -> None:
    module = load_script_module(
        "08_sync_med_device_fda_core.py",
        "med_device_fda_targeted_osur_test",
    )
    footprint_csv = tmp_path / "footprints.csv"
    fieldnames = [
        "ticker",
        "primary_fda_entity",
        "product_codes",
        "premarket_numbers",
        "expected_cdrh_records",
        "valid_from",
        "reviewed_at",
    ]
    with footprint_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "OSUR",
                "primary_fda_entity": "OraSure Technologies",
                "product_codes": "MIB;MZF",
                "premarket_numbers": "",
                "expected_cdrh_records": "yes",
                "valid_from": "2026-07-24",
                "reviewed_at": "2026-07-23",
            }
        )
        writer.writerow(
            {
                "ticker": "OSUR",
                "primary_fda_entity": "OraSure Technologies, Inc.",
                "product_codes": "MZO;QID;MZF",
                "premarket_numbers": "P080027;DEN190025",
                "expected_cdrh_records": "yes",
                "valid_from": "2026-08-10",
                "reviewed_at": "2026-08-10",
            }
        )

    prior = module.build_targeted_footprint_endpoints(
        footprint_csv,
        target_limit=1000,
        include_postmarket=True,
        tickers={"OSUR"},
        asof=date(2026, 8, 9),
    )
    effective = module.build_targeted_footprint_endpoints(
        footprint_csv,
        target_limit=1000,
        include_postmarket=True,
        tickers={"OSUR"},
        asof=date(2026, 8, 10),
    )
    prior_names = {endpoint.name for endpoint in prior}
    by_name = {endpoint.name: endpoint for endpoint in effective}

    assert "target_recall_code_OSUR_MIB" in prior_names
    assert "target_recall_code_OSUR_MIB" not in by_name
    assert by_name["target_510k_DEN190025"].search == 'k_number:"DEN190025"'
    assert by_name["target_pma_P080027"].search == 'pma_number:"P080027"'
    assert "target_event_code_OSUR_MZO" in by_name
    assert "target_event_code_OSUR_QID" in by_name
    assert "target_event_code_OSUR_MZF" in by_name
    assert by_name["target_event_code_OSUR_MZO"].search == (
        'device.device_report_product_code:"MZO" AND device.manufacturer_d_name:"OraSure Technologies, Inc."'
    )
    assert module.is_510k_or_denovo_identifier("DEN190025")


def test_fda_targeted_footprints_expand_governed_manufacturer_aliases(tmp_path: Path) -> None:
    module = load_script_module(
        "08_sync_med_device_fda_core.py",
        "med_device_fda_targeted_alias_test",
    )
    footprint_csv = tmp_path / "footprints.csv"
    alias_csv = tmp_path / "aliases.csv"
    footprint_csv.write_text(
        "ticker,primary_fda_entity,product_codes,expected_cdrh_records,valid_from,reviewed_at\n"
        'XRAY,"Dentsply Sirona, Inc.",NDP,yes,2026-08-10,2026-08-10\n',
        encoding="utf-8",
    )
    alias_csv.write_text(
        "ticker,alias_raw,valid_from,reviewed_at\nXRAY,DENTSPLY IH INC.,2026-08-10,2026-08-10\n",
        encoding="utf-8",
    )

    endpoints = module.build_targeted_footprint_endpoints(
        footprint_csv,
        target_limit=1000,
        include_postmarket=True,
        tickers={"XRAY"},
        asof=date(2026, 8, 12),
        alias_path=alias_csv,
    )
    searches = {endpoint.search for endpoint in endpoints if endpoint.path == "event.json"}

    assert 'device.manufacturer_d_name:"DENTSPLY IH INC."' in searches
    assert not any(
        'device.device_report_product_code:"NDP"' in search
        and 'device.manufacturer_d_name:"DENTSPLY IH INC."' in search
        for search in searches
    )
    assert (
        'device.device_report_product_code:"NDP" AND device.manufacturer_d_name:"Dentsply Sirona, Inc."'
    ) in searches
    assert 'device.manufacturer_d_name:"Dentsply Sirona, Inc."' in searches
    postmarket = [
        endpoint for endpoint in endpoints if endpoint.path in {"recall.json", "enforcement.json", "event.json"}
    ]
    assert postmarket
    assert all(endpoint.date_field for endpoint in postmarket)
    assert all(endpoint.window_days > 0 for endpoint in postmarket)
    assert all(endpoint.initial_lookback_days == 1096 for endpoint in postmarket)
    event_endpoints = [endpoint for endpoint in postmarket if endpoint.path == "event.json"]
    assert all(endpoint.window_days == 90 for endpoint in event_endpoints)
    assert all(endpoint.partition_field == "mdr_report_key" for endpoint in event_endpoints)


def test_canonical_mdr_family_dedup_is_deterministic_and_product_scoped() -> None:
    module = load_script_module(
        "78_build_med_device_fda_product_family_review.py",
        "med_device_fda_family_dedup_test",
    )
    base = {
        "asof_date": "2026-08-12",
        "ticker": "XRAY",
        "company_id": 64,
        "report_number": "2511302-2026-00001",
        "event_key": "",
        "source_available_date": "2026-08-01",
        "fda_manufacturer_id": "9252",
        "product_code": "NDP",
        "death_designated_flag": 0,
        "injury_flag": 1,
        "malfunction_flag": 0,
    }
    rows = [
        {**base, "adverse_event_id": "100", "mdr_report_key": "100"},
        {
            **base,
            "adverse_event_id": "101",
            "mdr_report_key": "101",
            "source_available_date": "2026-08-02",
            "malfunction_flag": 1,
        },
        {
            **base,
            "adverse_event_id": "102",
            "mdr_report_key": "102",
            "product_code": "NOF",
        },
    ]

    canonical = module.canonicalize_mdr_review_rows(rows)

    assert (
        canonical_mdr_family_key(
            {"report_number": "2511302-2026-00001"},
            "100",
        )
        == "report_number:2511302-2026-00001"
    )
    assert len(canonical) == 2
    ndp = next(row for row in canonical if row["product_code"] == "NDP")
    assert ndp["adverse_event_id"] == "101"
    assert ndp["canonical_family_member_count"] == 2
    assert ndp["canonical_family_member_ids"] == "100|101"


def test_fda_late_reports_use_event_date_for_clinical_recency(tmp_path: Path) -> None:
    feature_module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_fda_late_report_feature_test",
    )
    review_module = load_script_module(
        "78_build_med_device_fda_product_family_review.py",
        "med_device_fda_late_report_review_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector,
                country, currency, universe_status, is_active, first_seen_at,
                updated_at
            ) VALUES (
                1, 'XRAY', '0000000001', 'Dentsply Sirona', 'NASDAQ', 'dental',
                'United States', 'USD', 'active', 1, '2024-01-01', '2026-08-13'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dim_fda_manufacturer(
                fda_manufacturer_id, manufacturer_name, manufacturer_name_norm,
                parent_company_id, mapping_confidence, mapping_method,
                created_at, updated_at
            ) VALUES (
                10, 'DENTSPLY TEST', 'DENTSPLY TEST', 1, 99.0,
                'manual_override', '2026-08-13', '2026-08-13'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dim_fda_product_code(
                product_code, device_name, medical_specialty, device_class,
                regulation_number, source_id, created_at, updated_at
            ) VALUES (
                'DZE', 'Endosseous Dental Implant', 'DE', '2', '872.3640',
                NULL, '2026-08-13', '2026-08-13'
            )
            """
        )
        for event_id, event_date, report_date in (
            ("legacy", "2010-01-01", "2026-07-01"),
            ("current", "2026-01-01", "2026-02-01"),
            ("future_report", "2026-01-01", "2026-09-01"),
        ):
            conn.execute(
                """
                INSERT INTO fact_fda_adverse_event(
                    adverse_event_id, company_id, fda_manufacturer_id,
                    product_code, event_date, report_date, death_count,
                    injury_count, malfunction_count, event_type, payload_json,
                    created_at, updated_at
                ) VALUES (
                    ?, 1, 10, 'DZE', ?, ?, 0, 1, 0, 'Injury', ?,
                    '2026-08-13', '2026-08-13'
                )
                """,
                (
                    event_id,
                    event_date,
                    report_date,
                    json.dumps({"mdr_report_key": event_id, "report_number": event_id}),
                ),
            )

        policy = feature_module.fda_feature_policy(feature_module.load_yaml(REPO_ROOT / "med_devices" / "config.yaml"))
        feature_row = feature_module.FdaFeatureRow(
            asof_date="2026-08-13",
            company_id=1,
            ticker="XRAY",
            company_name="Dentsply Sirona",
        )
        feature_module.count_adverse_events(
            conn,
            feature_row,
            asof=date(2026, 8, 13),
            policy=policy,
        )
        review_rows = review_module.mdr_rows(
            conn,
            company_id=1,
            ticker="XRAY",
            asof=date(2026, 8, 13),
            window_start=date(2024, 8, 13),
            mappings=[],
            minimum_family_confidence=95.0,
            minimum_manufacturer_confidence=95.0,
        )

    assert feature_row.injury_count_24m == 1
    assert {row["adverse_event_id"] for row in review_rows} == {"current"}


def test_osur_regulatory_evidence_closes_data_fix_without_override() -> None:
    evidence_path = REPO_ROOT / "med_devices" / "data" / "fda_manual_footprint_evidence.csv"
    with evidence_path.open("r", encoding="utf-8-sig", newline="") as handle:
        evidence = {row["ticker"]: row for row in csv.DictReader(handle)}["OSUR"]
    assert evidence["fda_evidence_type"] == "pma_denovo_and_postmarket_mapped"
    assert evidence["evidence_confidence"] == "100"

    decision_path = REPO_ROOT / "med_devices" / "data" / "analyst_review_decisions.csv"
    decisions, issues = load_analyst_review_decisions(decision_path)
    assert not [issue for issue in issues if issue["severity"] == "CRITICAL"]
    prior = effective_decision(
        decisions,
        ticker="OSUR",
        cohort="diagnostics_clinical_tests",
        asof=date(2026, 8, 10),
    )
    effective = effective_decision(
        decisions,
        ticker="OSUR",
        cohort="diagnostics_clinical_tests",
        asof=date(2026, 8, 11),
    )
    assert prior is not None and prior.decision == "data_fix_needed"
    assert effective is not None and effective.decision == "watchlist"
    assert effective.review_category == "all"
    assert effective.allow_portfolio_candidate_override is False


def test_fda_manual_cber_approval_evidence_is_canonical_and_provenanced(
    tmp_path: Path,
) -> None:
    module = load_script_module(
        "08_sync_med_device_fda_core.py",
        "med_device_fda_manual_cber_test",
    )
    evidence_path = REPO_ROOT / "med_devices" / "data" / "fda_manual_approval_evidence.csv"
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        count = module.upsert_manual_approval_evidence(
            conn,
            evidence_path,
            source_id="fda_accessdata_cber",
        )
        rows = conn.execute(
            """
            SELECT submission_number, submission_type, product_code, decision_date,
                   source_id, payload_json
            FROM fact_fda_approval
            WHERE submission_number LIKE 'BP140143%'
            ORDER BY submission_number
            """
        ).fetchall()

    assert count == 2
    assert [row["submission_number"] for row in rows] == [
        "BP140143",
        "BP140143-S717",
    ]
    assert [row["submission_type"] for row in rows] == ["PMA", "PMA_SUPPLEMENT"]
    assert all(row["product_code"] == "PJF" for row in rows)
    assert all(row["source_id"] == "fda_accessdata_cber" for row in rows)
    assert all(
        json.loads(row["payload_json"])["evidence_method"] == "authoritative_manual_fda_accessdata" for row in rows
    )


def test_fda_core_parser_populates_canonical_tables(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_core_sync_test")
    key_file = tmp_path / "secrets.local.yaml"
    key_file.write_text('openfda_api_key: "test_key"\n', encoding="utf-8")
    policy = module.FdaPolicy(
        source_id="openfda_device",
        base_url="https://api.fda.gov/device",
        api_key_env="OPENFDA_API_KEY_NOT_SET",
        api_key_file=str(key_file),
        api_key_file_field="openfda_api_key",
        timeout_sec=30.0,
        max_retries=3,
        parallel_workers=1,
        sleep_sec=0.15,
        page_limit=1000,
        commit_every_pages=10,
        user_agent="test",
        endpoints=[],
    )
    assert module.resolve_api_key({}, policy=policy, base_dir=tmp_path) == "test_key"

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        module.upsert_classification(
            conn,
            {
                "product_code": "ABC",
                "device_name": "Cardiac Monitor",
                "medical_specialty": "Cardiovascular",
                "device_class": "2",
                "regulation_number": "870.2300",
            },
            source_id="openfda_device",
        )
        module.upsert_approval(
            conn,
            {
                "k_number": "K260001",
                "applicant": "Example Devices Inc.",
                "decision_date": "20260501",
                "date_received": "20260115",
                "decision_description": "Substantially Equivalent",
                "device_name": "Cardiac Monitor",
                "product_code": "ABC",
            },
            endpoint_name="approvals_510k",
            source_id="openfda_device",
        )
        module.upsert_approval(
            conn,
            {
                "k_number": "DEN250028",
                "applicant": "Spectralmd, Inc.",
                "decision_date": "20260521",
                "date_received": "20260306",
                "decision_description": "De Novo Granted",
                "device_name": "DeepView AI System",
                "product_code": "SHY",
            },
            endpoint_name="approvals_510k",
            source_id="openfda_device",
        )
        module.upsert_endpoint_records(
            conn,
            "target_pma_BP140143",
            [
                {
                    "pma_number": "BP140143",
                    "supplement_number": "S717",
                    "applicant": "Cerus Corporation",
                    "decision_date": "20221102",
                    "date_received": "20210930",
                    "decision": "Approved",
                    "trade_name": "INTERCEPT Blood System for Platelets",
                    "product_code": "PJF",
                }
            ],
            source_id="openfda_device",
        )
        module.upsert_endpoint_records(
            conn,
            "target_recall_code_CERS_PJF",
            [
                {
                    "recall_number": "Z-CERS-2026",
                    "recalling_firm": "Cerus Corporation",
                    "classification": "Class II",
                    "event_date_initiated": "20260601",
                    "product_code": "PJF",
                }
            ],
            source_id="openfda_device",
        )
        module.upsert_endpoint_records(
            conn,
            "target_event_code_CERS_PJF",
            [
                {
                    "mdr_report_key": "CERS-1",
                    "date_received": "20260620",
                    "event_type": "Injury",
                    "device": [
                        {
                            "manufacturer_d_name": "Cerus Corporation",
                            "device_report_product_code": "PJF",
                            "brand_name": "INTERCEPT Blood System for Platelets",
                        }
                    ],
                }
            ],
            source_id="openfda_device",
        )
        module.upsert_recall(
            conn,
            {
                "recall_number": "Z-0001-2026",
                "recalling_firm": "Example Devices Inc.",
                "classification": "Class I",
                "recall_initiation_date": "20260415",
                "reason_for_recall": "Test recall",
                "product_code": "ABC",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        module.upsert_adverse_event(
            conn,
            {
                "mdr_report_key": "123",
                "date_received": "20260420",
                "date_of_event": "20260418",
                "event_type": "Injury",
                "device": [
                    {
                        "manufacturer_d_name": "Example Devices Inc.",
                        "device_report_product_code": "ABC",
                        "brand_name": "Cardiac Monitor",
                    }
                ],
            },
            source_id="openfda_device",
        )
        product_row = conn.execute(
            "SELECT product_code FROM dim_fda_product_code WHERE product_code = 'ABC'"
        ).fetchone()
        approval_row = conn.execute(
            "SELECT decision_date FROM fact_fda_approval WHERE submission_number = 'K260001'"
        ).fetchone()
        denovo_row = conn.execute(
            "SELECT submission_type, product_code FROM fact_fda_approval WHERE submission_number = 'DEN250028'"
        ).fetchone()
        cber_row = conn.execute(
            "SELECT submission_type, product_code FROM fact_fda_approval WHERE submission_number = 'BP140143-S717'"
        ).fetchone()
        recall_row = conn.execute(
            "SELECT severity_weight FROM fact_fda_recall WHERE recall_number = 'Z-0001-2026'"
        ).fetchone()
        cber_recall_row = conn.execute(
            "SELECT endpoint_name FROM fact_fda_recall WHERE recall_number = 'Z-CERS-2026'"
        ).fetchone()
        event_row = conn.execute(
            "SELECT injury_count FROM fact_fda_adverse_event WHERE adverse_event_id = '123'"
        ).fetchone()
        cber_event_row = conn.execute(
            "SELECT injury_count FROM fact_fda_adverse_event WHERE adverse_event_id = 'CERS-1'"
        ).fetchone()

    assert product_row is not None
    assert approval_row is not None
    assert approval_row["decision_date"] == "2026-05-01"
    assert denovo_row is not None
    assert denovo_row["submission_type"] == "DENOVO"
    assert denovo_row["product_code"] == "SHY"
    assert cber_row is not None
    assert cber_row["submission_type"] == "PMA_SUPPLEMENT"
    assert cber_row["product_code"] == "PJF"
    assert recall_row is not None
    assert recall_row["severity_weight"] == 5.0
    assert cber_recall_row is not None
    assert cber_recall_row["endpoint_name"] == "target_recall_code_CERS_PJF"
    assert event_row is not None
    assert event_row["injury_count"] == 1
    assert cber_event_row is not None
    assert cber_event_row["injury_count"] == 1


def test_fda_adverse_event_counts_use_structured_fields_not_narrative_substrings() -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_structured_severity_test")

    assert module.event_counts(
        {
            "event_type": "Injury",
            "mdr_text": [{"text": "This failure mode has been adequately studied in the past."}],
        }
    ) == (0, 1, 0)
    assert module.event_counts(
        {
            "event_type": "Malfunction",
            "patient": [{"sequence_number_outcome": ["Other", " D", " H"]}],
        }
    ) == (1, 0, 1)
    assert module.event_counts(
        {
            "event_type": "Death",
            "mdr_text": [{"text": "The patient died later from an unrelated condition."}],
        }
    ) == (1, 0, 0)
    assert module.event_counts(
        {
            "event_type": "Malfunction",
            "mdr_text": [{"text": "The patient died later from an unrelated condition."}],
        }
    ) == (0, 0, 1)


def test_recompute_adverse_event_counts_repairs_stored_heuristic_values(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_recompute_severity_test")
    db_path = tmp_path / "med_devices.sqlite"
    payload = {
        "mdr_report_key": "PALTOP-1",
        "event_type": "Injury",
        "mdr_text": [{"text": "This failure mode has been adequately studied in the past."}],
    }
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO fact_fda_adverse_event(
                adverse_event_id, death_count, injury_count, malfunction_count,
                event_type, payload_json, created_at, updated_at
            )
            VALUES ('PALTOP-1', 1, 1, 1, 'Injury', ?, '2026-07-21', '2026-07-21')
            """,
            (json.dumps(payload),),
        )

        updated, invalid = module.recompute_adverse_event_counts(conn)
        row = conn.execute(
            """
            SELECT death_count, injury_count, malfunction_count
            FROM fact_fda_adverse_event
            WHERE adverse_event_id = 'PALTOP-1'
            """
        ).fetchone()

    assert updated == 1
    assert invalid == 0
    assert row is not None
    assert tuple(row) == (0, 1, 0)


def test_init_db_repairs_legacy_denovo_submission_type(tmp_path: Path) -> None:
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO fact_fda_approval(
                submission_number, submission_type, created_at, updated_at
            )
            VALUES ('DEN250028', '510k', '2026-05-21', '2026-05-21')
            """
        )
        init_db(conn)
        row = conn.execute(
            "SELECT submission_type FROM fact_fda_approval WHERE submission_number = 'DEN250028'"
        ).fetchone()

    assert row is not None
    assert row["submission_type"] == "DENOVO"


def test_raw_api_responses_are_run_scoped_after_legacy_migration(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_raw_response_test")
    db_path = tmp_path / "med_devices.sqlite"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE raw_api_responses (
            raw_response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            query_params_json TEXT,
            request_time_utc TEXT NOT NULL,
            response_status INTEGER,
            response_hash TEXT NOT NULL,
            asof_date TEXT,
            payload_text TEXT,
            ingestion_run_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, endpoint, response_hash)
        )
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    with connect(db_path) as conn:
        init_db(conn)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'raw_api_responses'"
        ).fetchone()
        index_names = {str(row["name"]) for row in conn.execute("PRAGMA index_list(raw_api_responses)").fetchall()}
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        run_1 = module.start_ingestion_run(conn, "openfda_device")
        with pytest.raises(RuntimeError, match="already running"):
            module.start_ingestion_run(conn, "openfda_device")
        module.finish_ingestion_run(
            conn,
            ingestion_run_id=run_1,
            status="success",
            request_count=0,
            row_count=0,
            message="test",
        )
        run_2 = module.start_ingestion_run(conn, "openfda_device")
        for run_id in (run_1, run_2):
            module.store_raw_response(
                conn,
                source_id="openfda_device",
                endpoint="https://api.fda.gov/device/recall.json",
                query_params={"limit": 1000, "skip": 0},
                response_status=200,
                payload_text='{"results":[{"recall_number":"Z-0001-2026"}]}',
                ingestion_run_id=run_id,
                asof_date="2026-08-10",
            )
        rows = conn.execute(
            """
            SELECT ingestion_run_id, response_hash
            FROM raw_api_responses
            WHERE source_id = 'openfda_device'
            ORDER BY ingestion_run_id
            """
        ).fetchall()

    assert table_sql is not None
    assert "UNIQUE(source_id, endpoint, response_hash)" not in str(table_sql["sql"])
    assert "idx_raw_api_responses_run_query" in index_names
    assert [int(row["ingestion_run_id"]) for row in rows] == [run_1, run_2]
    assert len({str(row["response_hash"]) for row in rows}) == 1


def test_fda_raw_response_replay_requires_complete_set_seal_and_valid_hash(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_replay_test")
    db_path = tmp_path / "med_devices.sqlite"
    payload_text = '{"meta":{"results":{"total":1}},"results":[{"recall_number":"Z-0001-2026"}]}'
    endpoint = module.EndpointConfig(
        name="recall",
        path="recall.json",
        enabled=True,
        search="",
        sort="",
        max_records=100,
    )
    policy = module.fda_policy(module.load_yaml(module.DEFAULT_CONFIG))
    public_params = {"limit": 100, "skip": 0}

    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        run_id = module.start_ingestion_run(conn, "openfda_device")
        module.store_raw_response(
            conn,
            source_id="openfda_device",
            endpoint=module.endpoint_url(policy, endpoint),
            query_params=public_params,
            response_status=200,
            payload_text=payload_text,
            ingestion_run_id=run_id,
            asof_date="2026-08-10",
        )
        unsealed = module.load_raw_response_replay_cache(
            conn,
            source_id="openfda_device",
            asof_date="2026-08-10",
        )
        module.finish_ingestion_run(
            conn,
            ingestion_run_id=run_id,
            status="success",
            request_count=1,
            row_count=1,
            message="test",
        )
        module.seal_ingestion_run(
            conn,
            ingestion_run_id=run_id,
            source_id="openfda_device",
            asof_date="2026-08-10",
        )
        same_day = module.load_raw_response_replay_cache(
            conn,
            source_id="openfda_device",
            asof_date="2026-08-10",
        )
        next_day = module.load_raw_response_replay_cache(
            conn,
            source_id="openfda_device",
            asof_date="2026-08-11",
        )

        page = module.fetch_fda_page_job(
            endpoint,
            policy=policy,
            api_key="not-used-for-replay",
            skip=0,
            limit=100,
            page_number_hint=1,
            replay_cache=same_day,
        )
        conn.execute("UPDATE raw_api_responses SET response_hash = 'tampered'")
        tampered = module.load_raw_response_replay_cache(
            conn,
            source_id="openfda_device",
            asof_date="2026-08-10",
        )

    assert unsealed == {}
    assert page.replayed is True
    assert page.payload["results"][0]["recall_number"] == "Z-0001-2026"
    assert next_day == {}
    assert tampered == {}


def test_fda_incremental_windows_are_anchored_scoped_and_watermarked(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_incremental_test")
    db_path = tmp_path / "med_devices.sqlite"
    endpoint = module.EndpointConfig(
        name="adverse_event",
        path="event.json",
        enabled=True,
        search="",
        sort="date_received:desc",
        max_records=25_000,
        date_field="date_received",
        window_days=7,
        overlap_days=5,
        initial_lookback_days=14,
    )

    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        first = module.plan_incremental_endpoints(
            conn,
            [endpoint],
            source_id="openfda_device",
            run_asof=date(2026, 8, 10),
        )
        repeated = module.plan_incremental_endpoints(
            conn,
            [endpoint],
            source_id="openfda_device",
            run_asof=date(2026, 8, 10),
        )
        run_id = module.start_ingestion_run(conn, "openfda_device")
        module.finish_ingestion_run(
            conn,
            ingestion_run_id=run_id,
            status="success",
            request_count=0,
            row_count=0,
            message="test",
        )
        module.seal_ingestion_run(
            conn,
            ingestion_run_id=run_id,
            source_id="openfda_device",
            asof_date="2026-08-10",
        )
        scope_hash = module.endpoint_scope_hash(endpoint)
        module.upsert_ingestion_watermark(
            conn,
            source_id="openfda_device",
            stream_name="adverse_event",
            scope_hash=scope_hash,
            date_field="date_received",
            watermark_date="2026-08-10",
            ingestion_run_id=run_id,
        )
        after_watermark = module.plan_incremental_endpoints(
            conn,
            [endpoint],
            source_id="openfda_device",
            run_asof=date(2026, 8, 11),
        )
        module.upsert_ingestion_watermark(
            conn,
            source_id="openfda_device",
            stream_name="adverse_event",
            scope_hash=scope_hash,
            date_field="date_received",
            watermark_date="2026-08-01",
            ingestion_run_id=run_id,
        )
        watermark = module.get_ingestion_watermark(
            conn,
            source_id="openfda_device",
            stream_name="adverse_event",
            scope_hash=scope_hash,
        )
        changed_scope = module.endpoint_scope_hash(module.replace(endpoint, search="event_type:Death"))
        missing_changed_scope = module.get_ingestion_watermark(
            conn,
            source_id="openfda_device",
            stream_name="adverse_event",
            scope_hash=changed_scope,
        )

    assert [(row.window_start, row.window_end, row.search) for row in first] == [
        (row.window_start, row.window_end, row.search) for row in repeated
    ]
    assert first[-1].window_end == "2026-08-10"
    assert all("date_received:[" in row.search for row in first)
    assert after_watermark[-1].window_end == "2026-08-11"
    assert watermark == date(2026, 8, 10)
    assert missing_changed_scope is None


def test_fda_global_adverse_template_is_replaced_by_governed_code_groups(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_scope_test")
    footprint = tmp_path / "footprint.csv"
    footprint.write_text(
        "ticker,product_codes,primary_fda_entity,valid_from,valid_to\n"
        "AAA,ZZZ;AAA,AAA,2020-01-01,\n"
        "BBB,BBB,BBB,2020-01-01,\n"
        "OLD,OLD,OLD,2020-01-01,2025-12-31\n",
        encoding="utf-8",
    )
    template = module.EndpointConfig(
        name="adverse_event",
        path="event.json",
        enabled=True,
        search="",
        sort="date_received:desc",
        max_records=25_000,
        date_field="date_received",
        window_days=1,
        overlap_days=14,
        initial_lookback_days=30,
        partition_field="mdr_report_key",
        partition_width=10_000,
        scope_product_code_field="device.device_report_product_code",
        scope_manufacturer_field="device.manufacturer_d_name",
        scope_group_size=2,
    )

    aliases = tmp_path / "aliases.csv"
    aliases.write_text(
        "ticker,alias_raw,valid_from,valid_to\nAAA,AAA Legacy,2020-01-01,\n",
        encoding="utf-8",
    )
    scoped = module.scope_endpoints_to_footprint_product_codes(
        [template], footprint, asof=date(2026, 8, 10), alias_path=aliases
    )

    assert [row.name for row in scoped] == [
        "target_event_group_001",
        "target_event_group_002",
        "target_event_group_003",
    ]
    assert "adverse_event" not in {row.name for row in scoped}
    combined_search = " ".join(row.search for row in scoped)
    assert all(f'device.device_report_product_code:"{code}"' in combined_search for code in ("AAA", "BBB", "ZZZ"))
    assert 'device.manufacturer_d_name:"AAA"' in combined_search
    assert 'device.manufacturer_d_name:"BBB"' in combined_search
    assert 'device.manufacturer_d_name:"AAA Legacy"' in combined_search
    assert "OLD" not in combined_search


def test_fda_overflow_is_partitioned_and_total_reconciled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_partition_test")
    db_path = tmp_path / "med_devices.sqlite"
    endpoint = module.EndpointConfig(
        name="adverse_event",
        path="event.json",
        enabled=True,
        search="date_received:[20260810 TO 20260810]",
        sort="date_received:desc",
        max_records=2,
        date_field="date_received",
        window_days=1,
        overlap_days=1,
        initial_lookback_days=1,
        stream_name="adverse_event",
        scope_hash="scope",
        window_start="2026-08-10",
        window_end="2026-08-10",
        partition_field="mdr_report_key",
        partition_width=2,
    )
    policy = module.fda_policy(module.load_yaml(module.DEFAULT_CONFIG))

    def fake_fetch(current: Any, **kwargs: Any) -> Any:
        current_endpoint = current
        sort = str(current_endpoint.sort)
        search = str(current_endpoint.search)
        if sort == "mdr_report_key:asc":
            keys = [1]
            total = 5
        elif sort == "mdr_report_key:desc":
            keys = [5]
            total = 5
        elif "mdr_report_key:[0 TO 1]" in search:
            keys = [1]
            total = 1
        elif "mdr_report_key:[2 TO 3]" in search:
            keys = [2, 3]
            total = 2
        elif "mdr_report_key:[4 TO 5]" in search:
            keys = [4, 5]
            total = 2
        else:
            keys = [1, 2]
            total = 5
        payload = {
            "meta": {"results": {"total": total}},
            "results": [{"mdr_report_key": str(key), "date_received": "20260810"} for key in keys],
        }
        skip = int(kwargs["skip"])
        limit = int(kwargs["limit"])
        public_params, _private = module.page_params(current_endpoint, skip=skip, limit=limit, api_key="")
        return module.FetchedFdaPage(
            endpoint_name=current_endpoint.name,
            url=module.endpoint_url(policy, current_endpoint),
            public_params=public_params,
            skip=skip,
            page_number_hint=int(kwargs["page_number_hint"]),
            response_status=200,
            payload_text=module.compact_json(payload),
            payload=payload,
        )

    monkeypatch.setattr(module, "fetch_fda_page_job", fake_fetch)
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        run_id = module.start_ingestion_run(conn, "openfda_device")
        result = module.sync_endpoint_with_partitions(
            conn,
            endpoint,
            policy=policy,
            api_key="",
            max_records=2,
            source_id="openfda_device",
            ingestion_run_id=run_id,
            asof_date="2026-08-10",
            replay_cache={},
            refresh_network=False,
        )
        raw_count = conn.execute(
            "SELECT COUNT(*) AS n FROM raw_api_responses WHERE ingestion_run_id = ?", (run_id,)
        ).fetchone()["n"]

    assert result.status == "success"
    assert result.reason == "deterministic_numeric_partition"
    assert result.total == 5
    assert result.seen == 5
    assert raw_count == 6


def test_fda_recall_lookup_uses_partial_index(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_recall_plan_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        exact_plan = conn.execute(
            "EXPLAIN QUERY PLAN " + module.FDA_RECALL_LOOKUP_SQL,
            ("recall_number:Z00012026", "openfda_device", "recall"),
        ).fetchall()
        legacy_plan = conn.execute(
            "EXPLAIN QUERY PLAN " + module.FDA_RECALL_LEGACY_ENDPOINT_LOOKUP_SQL,
            ("recall_number:Z00012026", "openfda_device"),
        ).fetchall()

    details = " ".join(str(row["detail"]) for row in [*exact_plan, *legacy_plan])
    assert "idx_fact_fda_recall_key_endpoint" in details
    assert "SCAN fact_fda_recall" not in details


def test_fda_linker_and_feature_builder_scores_mapped_records(tmp_path: Path) -> None:
    fda_module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_core_for_features_test")
    link_module = load_script_module("09_link_med_device_fda_to_companies.py", "med_device_fda_link_test")
    feature_module = load_script_module("10_build_med_device_fda_features.py", "med_device_fda_features_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'EXMD', '0000000001', 'Example Devices Inc.', 'NYSE', 'monitoring',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        fda_module.upsert_approval(
            conn,
            {
                "k_number": "K260001",
                "applicant": "Example Devices Incorporated",
                "decision_date": "20260501",
                "device_name": "Cardiac Monitor",
                "product_code": "ABC",
            },
            endpoint_name="approvals_510k",
            source_id="openfda_device",
        )
        fda_module.upsert_recall(
            conn,
            {
                "recall_number": "Z-0001-2026",
                "recalling_firm": "Example Devices Incorporated",
                "classification": "Class I",
                "recall_initiation_date": "20260415",
                "product_code": "ABC",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        aliases = link_module.build_aliases(conn)
        manufacturers = conn.execute(
            "SELECT fda_manufacturer_id, manufacturer_name FROM dim_fda_manufacturer"
        ).fetchall()
        for manufacturer in manufacturers:
            match = link_module.best_match(
                str(manufacturer["manufacturer_name"]),
                aliases,
                token_score_weight=100.0,
                min_confidence=75.0,
                edit_distance_max_normalized=0.20,
                edit_distance_score=70.0,
            )
            conn.execute(
                """
                UPDATE dim_fda_manufacturer
                SET parent_company_id = ?, mapping_confidence = ?, mapping_method = ?
                WHERE fda_manufacturer_id = ?
                """,
                (match.company_id, match.confidence, match.method, int(manufacturer["fda_manufacturer_id"])),
            )
        link_module.update_fact_company_ids(conn, min_confidence=75.0)
        feature_module.refresh_canonical_recalls(conn)
        companies = [feature_module.Company(company_id=1, ticker="EXMD", company_name="Example Devices Inc.")]
        policy = feature_module.FdaFeaturePolicy(
            source_id="openfda_device",
            short_months=12,
            medium_months=24,
            long_months=36,
            no_data_innovation_score=20.0,
            no_data_risk_score=65.0,
            revenue_floor=100000000.0,
            recall_decay_half_life_days=730.0,
            innovation_base_score=25.0,
            innovation_approval_log_weight=18.0,
            innovation_pma_log_weight=16.0,
            innovation_product_code_log_weight=12.0,
            risk_recall_severity_weight=4.0,
            risk_class_i_recall_weight=20.0,
            risk_death_per_billion_weight=5.0,
            risk_injury_per_billion_weight=0.5,
            risk_malfunction_per_billion_weight=0.1,
            risk_adverse_acceleration_per_billion_weight=0.5,
            min_mapping_confidence=75.0,
            class_i_lookback_months=36,
            death_lookback_months=24,
            death_event_min_count=1,
            class_i_hard_min_count=5,
            class_i_hard_min_severity_per_billion=10.0,
            death_event_hard_min_count=3,
            death_event_min_rate_per_billion=1.0,
            low_mapping_confidence_is_hard_red=False,
            regulatory_risk_weight=0.60,
            regulatory_innovation_weight=0.40,
        )
        asof = feature_module.parse_date("2026-05-22")
        assert asof is not None
        rows = feature_module.build_rows(conn, companies, asof=asof, policy=policy)

    assert len(rows) == 1
    assert rows[0].approval_count_12m == 1
    assert rows[0].class_i_recall_count_36m == 1
    assert rows[0].hard_red_flag == 1
    assert rows[0].fda_product_score is not None


def test_fda_canonical_recalls_collapse_product_rows_by_event_family(tmp_path: Path) -> None:
    fda_module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_event_family_core_test")
    feature_module = load_script_module(
        "10_build_med_device_fda_features.py",
        "med_device_fda_event_family_features_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'EXMD', '0000000001', 'Example Devices Inc.', 'NYSE', 'monitoring',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        fda_module.upsert_recall(
            conn,
            {
                "res_event_number": "96063",
                "firm_name": "Example Devices Inc.",
                "classification": "Class II",
                "event_date_initiated": "20250103",
                "product_code": "AAA",
            },
            endpoint_name="recalls",
            source_id="openfda_device",
        )
        fda_module.upsert_recall(
            conn,
            {
                "recall_number": "Z-1067-2025",
                "event_id": "96063",
                "recalling_firm": "Example Devices Inc.",
                "classification": "Class II",
                "recall_initiation_date": "20250103",
                "product_code": "AAA",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        fda_module.upsert_recall(
            conn,
            {
                "recall_number": "Z-1068-2025",
                "event_id": "96063",
                "recalling_firm": "Example Devices Inc.",
                "classification": "Class I",
                "recall_initiation_date": "20250103",
                "product_code": "AAB",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        conn.execute(
            """
            UPDATE dim_fda_manufacturer
            SET parent_company_id = 1, mapping_confidence = 100.0, mapping_method = 'test'
            """
        )
        conn.execute("UPDATE fact_fda_recall SET company_id = 1")

        canonical_count = feature_module.refresh_canonical_recalls(conn)
        canonical = conn.execute(
            """
            SELECT canonical_recall_key, classification, max_severity_weight, source_count, payload_json
            FROM fact_fda_recall_canonical
            """
        ).fetchone()

    assert canonical_count == 1
    assert canonical is not None
    assert canonical["canonical_recall_key"] == "event_id:96063"
    assert canonical["classification"] == "Class I"
    assert float(canonical["max_severity_weight"]) == 5.0
    assert int(canonical["source_count"]) == 3
    payload = json.loads(str(canonical["payload_json"]))
    assert len(payload["source_fda_recall_ids"]) == 3


def test_reimbursement_feature_builder_is_conservative_without_cms_data(tmp_path: Path) -> None:
    module = load_script_module(
        "11_build_med_device_reimbursement_features.py", "med_device_reimbursement_features_test"
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'EXMD', '0000000001', 'Example Devices Inc.', 'NYSE', 'monitoring',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        policy = module.ReimbursementPolicy(
            source_ids=["cms_coverage_api", "cms_payment_files"],
            no_data_score=25.0,
            no_data_coverage_clarity_score=25.0,
            no_data_payment_adequacy_score=25.0,
            company_mention_score=45.0,
            policy_evidence_score=60.0,
            rate_evidence_score=65.0,
            coverage_weight=0.50,
            payment_weight=0.50,
            mention_count_boost_per_hit=2.0,
            mention_count_boost_cap=10.0,
            low_confidence_hard_flag=False,
            use_fallback_policy_scan_when_unmapped=True,
            valid_no_rate_statuses={"not_applicable", "bundled", "unknown"},
        )
        rows = module.build_rows(
            conn,
            [module.Company(company_id=1, ticker="EXMD", company_name="Example Devices Inc.")],
            asof="2026-05-22",
            policy=policy,
        )

    assert len(rows) == 1
    assert rows[0].score == 25.0
    assert rows[0].review_reason == "cms_reimbursement_data_not_loaded"


def test_daily_scores_durable_proxy_uses_canonical_fcf_margin_field() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_proxy_test")
    assert "fcf_margin_ttm" in module.DURABLE_GROWTH_PROXY_INPUT_FIELDS
    assert "free_cash_flow_margin_ttm" not in module.DURABLE_GROWTH_PROXY_INPUT_FIELDS
    assert module.durable_proxy_available({"fcf_margin_ttm": "10", "gross_margin_ttm": "55"})
    row = module.ScoreRow(
        asof_date="2026-06-01",
        scoring_model_version="test",
        rank=0,
        company_id=1,
        ticker="AAA",
        company_name="AAA Medical",
        subsector="medical_devices",
    )
    assert row.durable_growth_validation_status == module.DURABLE_GROWTH_PRODUCTION_DISABLED
    assert row.durable_growth_production_state == module.DURABLE_GROWTH_PRODUCTION_DISABLED


def test_cohort_neutral_backtest_loads_fda_mapping_confidence_alias(tmp_path: Path) -> None:
    module = load_script_module(
        "23_backtest_med_device_cohort_neutral_scores.py",
        "med_device_cohort_neutral_scores_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'AAA', '0000000001', 'AAA Medical', 'NYSE', 'medical_devices',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO med_device_daily_scores(
                asof_date, company_id, scoring_model_version, composite_score, raw_composite_score,
                composite_percentile, created_at, updated_at
            )
            VALUES ('2026-06-01', 1, 'test', 55, 55, 50, '2026-06-01', '2026-06-01')
            """
        )
        conn.execute(
            """
            INSERT INTO feature_fda_product_risk(
                asof_date, company_id, avg_mapping_confidence, created_at, updated_at
            )
            VALUES ('2026-06-01', 1, 91.5, '2026-06-01', '2026-06-01')
            """
        )
        scores = module.load_scores(conn, asofs={"2026-06-01"})

    assert scores[("2026-06-01", "AAA")]["avg_fda_mapping_confidence"] == 91.5


def test_score_backtest_exports_stage11_metadata_and_point_in_time_cohort() -> None:
    module = load_script_module(
        "17_backtest_med_device_scores.py",
        "med_device_score_backtest_stage11_test",
    )
    rows = module.build_backtest_rows(
        [
            {
                "ticker": "AAA",
                "company_name": "AAA Medical",
                "subsector": "medical_devices",
                "calibration_cohort": "historical_cohort",
                "calibration_eligible_flag": 1,
                "research_calibration_input_eligible_flag": 1,
                "research_calibration_status": "eligible",
                "research_calibration_reason": "ok",
                "calibration_sample_role": "research_calibration_input",
                "stage11_calibration_input_eligible_flag": 1,
                "stage11_calibration_input_reason": "ok",
                "stage11_calibration_panel_source": "test_survivorship_panel",
                "survivorship_corrected_panel_flag": 1,
                "composite_score": 60.0,
                "raw_composite_score": 60.0,
                "composite_percentile": 50.0,
                "rank": 1,
                "final_investability_gate": 1,
                "portfolio_candidate_gate": 1,
            }
        ],
        {
            "AAA": (
                "yahoo_finance_backup",
                [(date(2024, 1, 2), 100.0), (date(2024, 1, 3), 101.0)],
            )
        },
        asof="2024-01-02",
        horizons=[1],
        position_usd=50_000.0,
    )

    assert len(rows) == 1
    assert rows[0]["calibration_cohort"] == "historical_cohort"
    assert rows[0]["final_investability_gate"] == 1
    assert rows[0]["portfolio_candidate_gate"] == 1
    assert rows[0]["calibration_eligible_flag"] == 1
    assert rows[0]["stage11_calibration_input_eligible_flag"] == 1
    assert rows[0]["stage11_calibration_panel_source"] == "test_survivorship_panel"
    assert rows[0]["survivorship_corrected_panel_flag"] == 1
    assert module.flag_is_one(1)
    assert module.flag_is_one("1")
    assert not module.flag_is_one(0)
    assert not module.flag_is_one(None)


def test_stage11_filter_can_exclude_an_all_ineligible_asof_without_dropping_valid_dates() -> None:
    module = load_script_module(
        "17_backtest_med_device_scores.py",
        "med_device_score_backtest_stage11_filter_test",
    )
    score_rows_by_asof = {
        "2024-01-02": [{"stage11_calibration_input_eligible_flag": 1}],
        "2024-01-03": [{"stage11_calibration_input_eligible_flag": 0}],
    }

    filtered, empty_asofs = module.filter_stage11_eligible_rows(score_rows_by_asof)

    assert list(filtered) == ["2024-01-02"]
    assert empty_asofs == ["2024-01-03"]


def test_cohort_neutral_backtest_prefers_saved_point_in_time_cohort() -> None:
    module = load_script_module(
        "23_backtest_med_device_cohort_neutral_scores.py",
        "med_device_cohort_neutral_pit_cohort_test",
    )
    rows = [{"asof_date": "2024-01-02", "ticker": "AAA"}]
    taxonomy = {"AAA": {"calibration_cohort": "current_cohort"}}
    scores = {
        ("2024-01-02", "AAA"): {
            "calibration_cohort": "historical_cohort",
            "scoring_model_version": "test",
        }
    }

    module.add_taxonomy_and_scores(rows, taxonomy, scores)

    assert rows[0]["calibration_cohort"] == "historical_cohort"


def test_optuna_cross_horizon_hit_rate_and_sampling_tolerance() -> None:
    module = load_script_module(
        "69_optimize_med_device_optuna_policies.py",
        "med_device_optuna_guardrail_test",
    )
    settings = {
        "hit_rate_test": "cross_horizon_average",
        "horizons": [60, 120],
        "min_excess_hit_rate": 0.52,
    }

    assert (
        module.cross_horizon_hit_rate_guardrail_reason(
            [{"excess_hit_rate": 0.4915}, {"excess_hit_rate": 0.6780}],
            settings,
        )
        == ""
    )
    assert module.cross_horizon_hit_rate_guardrail_reason(
        [{"excess_hit_rate": 0.49}, {"excess_hit_rate": 0.50}],
        settings,
    ).startswith("cross_horizon_hit_rate_0.49_below_0.52")

    effective_limit = module.effective_single_ticker_share_limit(0.35, 136, 0.01)
    assert effective_limit == pytest.approx(0.35 + (1.0 / 136.0))
    assert 0.352941 < effective_limit
    assert module.effective_single_ticker_share_limit(0.35, 0, 0.01) == 0.35


def test_hospital_baseline_promotion_is_effective_dated_and_uses_aliased_gates() -> None:
    scoring_module = load_script_module(
        "13_build_med_device_daily_scores.py",
        "med_device_effective_dated_baseline_test",
    )
    publishing_module = load_script_module(
        "16_publish_med_device_score_review_pack.py",
        "med_device_effective_dated_baseline_publish_test",
    )
    cohort = "hospital_supplies_surgical_consumables_oem"
    config = {
        "scoring": {
            "gates": {"composite_min": 75.0},
            "cohort_profile_aliases": {cohort: "hospital_supplies_consumables_dme"},
            "cohort_profiles": {
                "hospital_supplies_consumables_dme": {
                    "gates": {"composite_min": 50.0},
                }
            },
        },
        "calibration": {
            "calibrated_baseline": {
                "production_seed_cohorts": cohort,
                "watchlist_seed_cohorts": "",
                "production_seed_effective_from": {cohort: "2026-07-17"},
            }
        },
    }
    row = scoring_module.ScoreRow(
        asof_date="2026-07-16",
        scoring_model_version="test",
        rank=1,
        company_id=1,
        ticker="AAA",
        company_name="AAA Medical",
        subsector="medical_devices",
        calibration_cohort=cohort,
        passed_fda_manual_review_gate=1,
        composite_score=55.0,
    )
    gates = {"composite_min": 50.0}

    assert scoring_module.calibrated_baseline_candidate_status(row, config=config, gates=gates) is None
    row.asof_date = "2026-07-17"
    assert scoring_module.calibrated_baseline_candidate_status(
        row,
        config=config,
        gates=gates,
    ) == ("calibrated_baseline", "baseline_gate_pass_not_tier1")

    publish_row = {
        "asof_date": "2026-07-17",
        "calibration_cohort": cohort,
        "classification": "unclassified",
        "passed_fda_manual_review_gate": 1,
        "hard_red_flag": 0,
        "raw_composite_score": 55.0,
    }
    assert publishing_module.configured_gate_value(config, cohort, "composite_min") == 50.0
    assert publishing_module.calibrated_baseline_candidate_status(
        publish_row,
        config,
    ) == ("production_baseline_candidate", "baseline_gate_pass_not_tier1")

    row.passed_data_quality_gate = 1
    row.passed_liquidity_gate = 1
    row.hard_red_flag = 1
    scoring_module.apply_portfolio_candidate_policy(row, config=config, gates=gates)
    assert row.portfolio_candidate_gate == 0
    assert row.portfolio_candidate_reason.startswith("fda_manual_review_or_hard_red")


def test_daily_score_template_tier1_metadata_is_explicit() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_tier1_template_test")

    safe_template = module.parse_score_template(
        {
            "template_id": "safe_quality_value",
            "tier1_role": "safe_core",
            "tier1_eligible": True,
            "components": [
                {"field": "fundamental_quality_score", "direction": "positive", "weight": 0.6},
                {"field": "valuation_score", "direction": "positive", "weight": 0.4},
            ],
        },
        context="test.safe_template",
    )
    special_template = module.parse_score_template(
        {
            "template_id": "pullback_research",
            "tier1_role": "special_situation",
            "tier1_eligible": True,
            "components": [
                {"field": "technical_pullback_score", "direction": "positive", "weight": 1.0},
            ],
        },
        context="test.special_template",
    )

    assert safe_template.tier1_role == module.TIER1_TEMPLATE_ROLE_SAFE_CORE
    assert safe_template.tier1_eligible is True
    assert "role=safe_core;tier1_eligible=1" in module.score_template_spec(safe_template)
    assert special_template.tier1_role == module.TIER1_TEMPLATE_ROLE_SPECIAL_SITUATION
    assert special_template.tier1_eligible is False


def test_daily_score_tier1_safety_gate_routes_special_situations() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_tier1_safety_test")
    row = module.ScoreRow(
        asof_date="2026-06-01",
        scoring_model_version="test",
        rank=0,
        company_id=1,
        ticker="TLSI",
        company_name="TriSalus Life Sciences",
        subsector="medical_devices",
        raw_composite_score=85.0,
        composite_percentile=95.0,
        cohort_percentile=95.0,
        calibration_cohort="implantable_interventional_devices_procedure_bundled",
        cohort_score_template_id="procedure_bundled_pullback_fda_risk_only",
        cohort_score_template_spec=(
            "role=special_situation;tier1_eligible=0;"
            "technical_pullback_score:positive:0.45;valuation_score:inverse:0.20"
        ),
        cohort_score_template_tier1_role=module.TIER1_TEMPLATE_ROLE_SPECIAL_SITUATION,
        cohort_score_template_tier1_eligible=0,
        single_product_risk_flag=1,
        binary_event_risk_flag=1,
        fundamental_quality_score=80.0,
        durable_growth_score=70.0,
        fda_product_score=80.0,
        fda_event_risk_score=10.0,
        reimbursement_score=70.0,
        reimbursement_status="direct_payment_evidence",
        unknown_reimbursement_flag=0,
        valuation_score=80.0,
        technical_entry_score=70.0,
        technical_entry_status_score=70.0,
        value_trap_score=5.0,
        data_completeness_score=100.0,
        avg_dollar_volume_60d=10_000_000.0,
        market_cap=2_000_000_000.0,
        fda_data_available=1,
    )
    gates = {
        "composite_min": 75.0,
        "cohort_percentile_min": 0.0,
        "fundamental_quality_min": 70.0,
        "durable_growth_min": 60.0,
        "fda_product_min": 60.0,
        "reimbursement_min": 45.0,
        "valuation_min": 60.0,
        "technical_entry_min": 55.0,
        "data_completeness_min": 90.0,
        "min_avg_dollar_volume_60d": 1_000_000.0,
        "watchlist_min": 60.0,
        "value_trap_max": 20.0,
        "value_trap_hard_max": 85.0,
    }
    policy = module.Tier1SafetyPolicy(
        min_market_cap=500_000_000.0,
        min_avg_dollar_volume_60d=2_000_000.0,
        ticker_denylist=("tlsi",),
    )

    module.classify(row, gates=gates, tier1_policy=policy)

    assert row.passed_tier1_safety_gate == 0
    assert row.tier1_safety_status == module.TIER1_SAFETY_STATUS_FAIL
    assert row.final_investability_gate == 0
    assert row.classification == "special_situation_or_binary_risk_watchlist"
    assert "template_not_safe_core" in row.tier1_safety_reason
    assert "single_product_risk" in row.tier1_safety_reason
    assert "ticker_denylist" in row.tier1_safety_reason
