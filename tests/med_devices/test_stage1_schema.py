from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from med_devices.core.analyst_review import effective_decision, load_analyst_review_decisions
from med_devices.core.db import connect, init_db
from med_devices.core.fda_product_family_review import (
    build_product_family_shadow_score,
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


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


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
    assert effective["VCYT"]["footprint_category"] == (
        "centralized_ldt_clia_with_dormant_legacy_clearance"
    )
    assert effective["VCYT"]["premarket_numbers"] == "K130010"
    assert effective["VCYT"]["product_codes"] == "NYI;NSU"
    assert effective["VCYT"]["expected_cdrh_records"] == "legacy_only"


def test_vnrx_incorrect_cpt_expires_before_structural_lab_routing() -> None:
    reimbursement_module = load_script_module(
        "11_build_med_device_reimbursement_features.py",
        "med_device_vnrx_reimbursement_classification_test",
    )
    classification_path = (
        REPO_ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    )
    override_path = (
        REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"
    )

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
    assert effective["VNRX"].billing_category == (
        "veterinary_commercial_and_precommercial_human"
    )
    assert effective["VNRX"].payment_rate_status == "veterinary_no_cms"
    assert effective["VNRX"].primary_payment_file == "none"
    assert row_is_effective_asof(vnrx_code, date(2026, 7, 23))
    assert not row_is_effective_asof(vnrx_code, date(2026, 7, 24))


def test_wgs_reimbursement_anchor_transitions_to_exome_genome_code_set() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_mapping_overrides.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["ticker"] == "WGS"]

    before = {
        row["reimbursement_code"]
        for row in rows
        if row_is_effective_asof(row, date(2026, 7, 23))
    }
    effective = {
        row["reimbursement_code"]
        for row in rows
        if row_is_effective_asof(row, date(2026, 7, 24))
    }

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
            if row["ticker"] == ticker
            and row_is_effective_asof(row, date(2026, 7, 24))
        }
        assert current_codes == {code}

    gral_codes = {
        row["reimbursement_code"]
        for row in rows
        if row["ticker"] == "GRAL"
        and row_is_effective_asof(row, date(2026, 7, 24))
    }
    assert not gral_codes


def test_generic_81479_has_no_ticker_agnostic_manual_flat_rate() -> None:
    path = REPO_ROOT / "med_devices" / "data" / "reimbursement_manual_payment_rates.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert not [
        row
        for row in rows
        if row["code"] == "81479" and str(row["payment_rate"] or "").strip()
    ]


def test_updated_lab_reimbursement_classifications_are_effective() -> None:
    module = load_script_module(
        "11_build_med_device_reimbursement_features.py",
        "med_device_updated_lab_reimbursement_classification_test",
    )
    path = (
        REPO_ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    )
    classifications = module.load_company_classifications(
        path,
        asof=date(2026, 7, 24),
    )

    assert classifications["NEO"].billing_category == "laboratory_services"
    assert classifications["NEO"].payment_rate_status == "large_lab_clfs_array"
    assert classifications["PSNL"].primary_payment_file == "cms_moldx_mrd"
    assert classifications["GRAL"].payment_rate_status == "cash_pay_or_out_of_pocket"
    assert classifications["XGN"].primary_payment_file == "cms_clfs_pla_0312u"


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
    assert {
        mapping.product_code
        for mapping in effective
        if mapping.ticker == "ABT"
    }.issuperset({"DSQ", "QBJ", "QLG", "NGV", "NKM", "OAE", "DQK"})
    governed_families = {
        exposure.product_family
        for exposure in exposures
        if exposure.ticker == "ABT"
    }
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
    available = {
        exposure.product_family: exposure
        for exposure in exposures
        if exposure.exposure_status == "available"
    }
    assert len(available) == 8
    assert available["diabetes_cgm"].exposure_value == 7600.0
    assert available["diabetes_cgm"].exposure_scope == "product_family"
    assert available["lvad_circulatory_support"].exposure_scope == (
        "business_subsegment_fallback"
    )
    assert available["lvad_circulatory_support"].exposure_confidence == 75.0
    waived = {
        exposure.product_family
        for exposure in exposures
        if exposure.exposure_status == "waived_no_specific_exposure"
    }
    assert waived == {"cardiopulmonary_surgical_support"}


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
        item
        for item in shadow.family_details
        if item["product_family"] == "cardiopulmonary_surgical_support"
    )
    assert waived_detail["denominator_usd_millions"] == 500.0
    assert (
        waived_detail["denominator_source"]
        == "governed_waiver_conservative_floor"
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
        price_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(fact_price_ohlcv)").fetchall()
        }
        fda_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(feature_fda_product_risk)"
            ).fetchall()
        }
        score_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(med_device_daily_scores)"
            ).fetchall()
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
    assert "fda_event_risk_product_family_adjusted_score" in score_columns
    assert "fda_safety_product_family_adjusted_score" in score_columns
    assert "fda_product_family_shadow_oos_valid_flag" in score_columns


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
                column
                for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS
                if column not in fresh_columns[table]
            ]
            assert not missing_from_create, (
                f"{table} CREATE TABLE is missing shadow columns: {missing_from_create}"
            )
            for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            pre_migration = table_columns(conn, table)
            assert not pre_migration.intersection(FDA_PRODUCT_FAMILY_SHADOW_COLUMNS)

        init_db(conn)

        for table in tables:
            migrated = table_columns(conn, table)
            not_migrated = [
                column
                for column in FDA_PRODUCT_FAMILY_SHADOW_COLUMNS
                if column not in migrated
            ]
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
                "ISRG,\"Intuitive Surgical, Inc.\",Healthcare,Medical Instruments & Supplies,0001035267,Nasdaq,Common Stock,active,TRUE,United States,USD,INTUITIVE SURGICAL INC,ISRG,exact,sec,nasdaqtrader,,false,false,false,",
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
        companies = [module.Company(company_id=1, ticker="AAA", company_name="AAA Medical", subsector="medical_devices")]
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
        product_row = conn.execute("SELECT product_code FROM dim_fda_product_code WHERE product_code = 'ABC'").fetchone()
        approval_row = conn.execute("SELECT decision_date FROM fact_fda_approval WHERE submission_number = 'K260001'").fetchone()
        denovo_row = conn.execute(
            "SELECT submission_type, product_code FROM fact_fda_approval WHERE submission_number = 'DEN250028'"
        ).fetchone()
        recall_row = conn.execute("SELECT severity_weight FROM fact_fda_recall WHERE recall_number = 'Z-0001-2026'").fetchone()
        event_row = conn.execute("SELECT injury_count FROM fact_fda_adverse_event WHERE adverse_event_id = '123'").fetchone()

    assert product_row is not None
    assert approval_row is not None
    assert approval_row["decision_date"] == "2026-05-01"
    assert denovo_row is not None
    assert denovo_row["submission_type"] == "DENOVO"
    assert denovo_row["product_code"] == "SHY"
    assert recall_row is not None
    assert recall_row["severity_weight"] == 5.0
    assert event_row is not None
    assert event_row["injury_count"] == 1


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
        index_names = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(raw_api_responses)").fetchall()
        }
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        run_1 = module.start_ingestion_run(conn, "openfda_device")
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
        manufacturers = conn.execute("SELECT fda_manufacturer_id, manufacturer_name FROM dim_fda_manufacturer").fetchall()
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
    module = load_script_module("11_build_med_device_reimbursement_features.py", "med_device_reimbursement_features_test")
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
