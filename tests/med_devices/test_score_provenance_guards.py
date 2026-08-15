from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

from med_devices.core.db import connect, init_db
from portfolio_layer.scores.adapters import _adapt_med_devices


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "med_devices" / "scripts"


def load_script(filename: str, module_name: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def locked_policy() -> dict[str, object]:
    return {
        "phase1_safety_lock": True,
        "production_score_regime_effective_from": "2026-07-27",
        "locked_production_score_regime_version": (
            "med_devices_baseline_composite_shadow_locked_v2_20260727"
        ),
        "locked_scoring_model_version": (
            "med_device_score_v24_2026_07_hospital_supplies_promotion_shadow_lock_v2_20260727"
        ),
    }


def test_score_builder_publishes_source_flag_and_cutover_model_version() -> None:
    module = load_script(
        "13_build_med_device_daily_scores.py",
        "med_device_score_provenance_builder_test",
    )
    pre_cutover = module.ScoreRow(
        asof_date="2026-07-24",
        scoring_model_version="original",
        score_model_version="original",
        model_version="original",
        ic_tilted_composite_mode="shadow",
    )
    module.apply_production_score_provenance(pre_cutover, policy=locked_policy())

    assert pre_cutover.production_score_source == "baseline_composite_score"
    assert pre_cutover.ic_tilt_applied_to_production_flag == 0
    assert pre_cutover.production_score_regime_version == (
        "med_devices_baseline_composite_v1_pre_20260727"
    )
    assert pre_cutover.scoring_model_version == "original"

    post_cutover = module.ScoreRow(
        asof_date="2026-07-27",
        scoring_model_version="original",
        score_model_version="original",
        model_version="original",
        ic_tilted_composite_mode="shadow",
    )
    module.apply_production_score_provenance(post_cutover, policy=locked_policy())

    assert post_cutover.production_score_source == "baseline_composite_score"
    assert post_cutover.ic_tilt_applied_to_production_flag == 0
    assert post_cutover.production_score_regime_version == (
        "med_devices_baseline_composite_shadow_locked_v2_20260727"
    )
    assert post_cutover.scoring_model_version.endswith("_shadow_lock_v2_20260727")
    assert post_cutover.score_model_version == post_cutover.scoring_model_version
    assert post_cutover.model_version == post_cutover.scoring_model_version

    unsafe = module.ScoreRow(
        asof_date="2026-07-24",
        scoring_model_version="legacy",
        score_model_version="legacy",
        model_version="legacy",
        ic_tilted_composite_mode="replace_raw",
        native_score_value=60.0,
        composite_score=60.0,
        live_component_count=1,
    )
    module.apply_production_score_provenance(unsafe, policy={**locked_policy(), "phase1_safety_lock": False})
    module.apply_research_calibration_metadata(unsafe, oos_score_valid=True)

    assert unsafe.production_score_source == "ic_tilted_composite_score"
    assert unsafe.ic_tilt_applied_to_production_flag == 1
    assert unsafe.production_score_regime_version == "med_devices_ic_tilt_replace_legacy_v1"
    assert unsafe.oos_score_valid_flag == 0
    assert unsafe.research_calibration_input_eligible_flag == 0
    assert unsafe.stage11_calibration_input_eligible_flag == 0
    assert "unsafe_ic_tilt_applied_to_production" in unsafe.research_calibration_reason


def insert_company(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO dim_company(
            company_id, ticker, cik, company_name, exchange, subsector,
            country, currency, universe_status, is_active, first_seen_at, updated_at
        )
        VALUES (1, 'AAA', '0000000001', 'AAA Medical', 'NYSE', 'medical_devices',
                'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
        """
    )


def insert_score_row(
    conn: sqlite3.Connection,
    *,
    asof: str,
    mode: str,
    source: str,
    applied: int,
    oos_valid: int,
    sample_role: str,
) -> None:
    conn.execute(
        """
        INSERT INTO med_device_daily_scores(
            asof_date, company_id, scoring_model_version, score_model_version,
            composite_score, raw_composite_score, composite_percentile,
            native_score_value, source_snapshot_asof_date,
            research_calibration_input_eligible_flag, research_calibration_status,
            research_calibration_reason, stage11_calibration_input_eligible_flag,
            stage11_calibration_input_reason, stage11_calibration_panel_source,
            survivorship_corrected_panel_flag, calibration_only, score_zero_is_missing_flag,
            calibration_sample_role, oos_score_valid_flag, ic_tilted_composite_mode,
            production_score_source, ic_tilt_applied_to_production_flag,
            production_score_regime_version, created_at, updated_at
        )
        VALUES (
            ?, 1, 'test', 'test', 60.0, 60.0, 50.0, 60.0, ?,
            1, 'eligible', 'valid_research_calibration_input', 1, 'ok',
            'med_devices_survivorship_corrected_score_review_pack', 1, 0, 0,
            ?, ?, ?, ?, ?, 'test_regime', '2026-01-01', '2026-01-01'
        )
        """,
        (asof, asof, sample_role, oos_valid, mode, source, applied),
    )


def test_score_builder_upsert_round_trips_provenance_fields(tmp_path: Path) -> None:
    module = load_script(
        "13_build_med_device_daily_scores.py",
        "med_device_score_provenance_upsert_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    row = module.ScoreRow(
        asof_date="2026-08-11",
        scoring_model_version="locked",
        score_model_version="locked",
        model_version="locked",
        company_id=1,
        ticker="AAA",
        company_name="AAA Medical",
        subsector="medical_devices",
        composite_score=60.0,
        raw_composite_score=60.0,
        native_score_value=60.0,
        production_score_source="baseline_composite_score",
        ic_tilt_applied_to_production_flag=0,
        production_score_regime_version=(
            "med_devices_baseline_composite_shadow_locked_v2_20260727"
        ),
    )
    with connect(db_path) as conn:
        init_db(conn)
        insert_company(conn)
        assert module.upsert_rows(conn, [row]) == 1
        persisted = conn.execute(
            """
            SELECT production_score_source, ic_tilt_applied_to_production_flag,
                   production_score_regime_version
            FROM med_device_daily_scores
            WHERE asof_date = '2026-08-11' AND company_id = 1
            """
        ).fetchone()

    assert persisted["production_score_source"] == "baseline_composite_score"
    assert persisted["ic_tilt_applied_to_production_flag"] == 0
    assert persisted["production_score_regime_version"] == (
        "med_devices_baseline_composite_shadow_locked_v2_20260727"
    )


def test_oos_promoter_accepts_clean_shadow_and_demotes_replace_raw(tmp_path: Path) -> None:
    module = load_script(
        "76_mark_med_device_oos_provenance.py",
        "med_device_score_provenance_oos_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        insert_company(conn)
        insert_score_row(
            conn,
            asof="2026-07-27",
            mode="shadow",
            source="baseline_composite_score",
            applied=0,
            oos_valid=0,
            sample_role="research_calibration_input",
        )
        insert_score_row(
            conn,
            asof="2026-07-24",
            mode="replace_raw",
            source="ic_tilted_composite_score",
            applied=1,
            oos_valid=1,
            sample_role="strict_oos",
        )

        assert module.promote_asof(conn, asof="2026-07-27", dry_run=False) == 1
        assert module.promote_asof(conn, asof="2026-07-24", dry_run=False) == 0
        assert module.demote_unsafe_asof(conn, asof="2026-07-24", dry_run=False) == 1

        clean = conn.execute(
            "SELECT oos_score_valid_flag, calibration_sample_role FROM med_device_daily_scores "
            "WHERE asof_date = '2026-07-27'"
        ).fetchone()
        unsafe = conn.execute(
            """
            SELECT oos_score_valid_flag, calibration_sample_role,
                   research_calibration_input_eligible_flag,
                   stage11_calibration_input_eligible_flag,
                   research_calibration_reason
            FROM med_device_daily_scores
            WHERE asof_date = '2026-07-24'
            """
        ).fetchone()

    assert clean["oos_score_valid_flag"] == 1
    assert clean["calibration_sample_role"] == "strict_oos"
    assert unsafe["oos_score_valid_flag"] == 0
    assert unsafe["calibration_sample_role"] == "excluded_from_research_calibration"
    assert unsafe["research_calibration_input_eligible_flag"] == 0
    assert unsafe["stage11_calibration_input_eligible_flag"] == 0
    assert unsafe["research_calibration_reason"] == "unsafe_production_score_provenance"


def test_unsafe_snapshot_demotion_is_targeted_and_idempotent(tmp_path: Path) -> None:
    module = load_script(
        "76_mark_med_device_oos_provenance.py",
        "med_device_score_provenance_csv_demotion_test",
    )
    path = tmp_path / "scores.csv"
    fieldnames = [
        "asof_date",
        "ticker",
        "ic_tilted_composite_mode",
        "production_score_source",
        "ic_tilt_applied_to_production_flag",
        "oos_score_valid_flag",
        "research_calibration_input_eligible_flag",
        "research_calibration_status",
        "research_calibration_reason",
        "stage11_calibration_input_eligible_flag",
        "stage11_calibration_input_reason",
        "calibration_sample_role",
    ]
    rows = [
        {
            "asof_date": "2026-07-24",
            "ticker": "CLEAN",
            "ic_tilted_composite_mode": "shadow",
            "production_score_source": "baseline_composite_score",
            "ic_tilt_applied_to_production_flag": "0",
            "oos_score_valid_flag": "1",
            "research_calibration_input_eligible_flag": "1",
            "research_calibration_status": "eligible",
            "research_calibration_reason": "valid_research_calibration_input",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "calibration_sample_role": "strict_oos",
        },
        {
            "asof_date": "2026-07-24",
            "ticker": "UNSAFE",
            "ic_tilted_composite_mode": "replace_raw",
            "production_score_source": "ic_tilted_composite_score",
            "ic_tilt_applied_to_production_flag": "1",
            "oos_score_valid_flag": "1",
            "research_calibration_input_eligible_flag": "1",
            "research_calibration_status": "eligible",
            "research_calibration_reason": "valid_research_calibration_input",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "calibration_sample_role": "strict_oos",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    assert module.demote_unsafe_snapshot_csv(path, dry_run=False) == (1, "ok")
    assert module.demote_unsafe_snapshot_csv(path, dry_run=False) == (0, "ok")

    with path.open("r", encoding="utf-8", newline="") as handle:
        by_ticker = {row["ticker"]: row for row in csv.DictReader(handle)}
    assert by_ticker["CLEAN"]["oos_score_valid_flag"] == "1"
    assert by_ticker["CLEAN"]["calibration_sample_role"] == "strict_oos"
    assert by_ticker["UNSAFE"]["oos_score_valid_flag"] == "0"
    assert (
        by_ticker["UNSAFE"]["calibration_sample_role"]
        == "excluded_from_research_calibration"
    )
    assert (
        by_ticker["UNSAFE"]["research_calibration_reason"]
        == "unsafe_production_score_provenance"
    )


def validator_row(module: ModuleType, *, unsafe: bool) -> dict[str, str]:
    fields = (
        module.REQUIRED_DAILY_COLUMNS
        | module.SCORE_PROVENANCE_DAILY_COLUMNS
        | {"score_model_version"}
    )
    row = {field: "" for field in fields}
    row.update(
        {
            "asof_date": "2026-08-11",
            "ticker": "AAA",
            "score_model_version": "test",
            "composite_score": "60",
            "calibration_cohort": "medical_devices",
            "calibration_eligible_flag": "1",
            "portfolio_candidate_gate": "1",
            "score_scale_min": "0",
            "score_scale_max": "100",
            "score_neutral_value": "50",
            "oos_score_valid_flag": "1",
            "native_score_value": "60",
            "research_calibration_input_eligible_flag": "1",
            "research_calibration_status": "eligible",
            "research_calibration_reason": "valid_research_calibration_input",
            "calibration_sample_role": "strict_oos",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "stage11_calibration_panel_source": "med_devices_survivorship_corrected_score_review_pack",
            "survivorship_corrected_panel_flag": "1",
            "source_snapshot_asof_date": "2026-08-11",
            "ic_tilted_composite_mode": "replace_raw" if unsafe else "shadow",
            "production_score_source": (
                "ic_tilted_composite_score" if unsafe else "baseline_composite_score"
            ),
            "ic_tilt_applied_to_production_flag": "1" if unsafe else "0",
            "production_score_regime_version": (
                "med_devices_ic_tilt_replace_legacy_v1"
                if unsafe
                else "med_devices_baseline_composite_shadow_locked_v2_20260727"
            ),
        }
    )
    return row


def write_validator_csv(path: Path, row: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(row))
        writer.writeheader()
        writer.writerow(row)


def check_by_id(checks: list[dict[str, object]], check_id: str) -> dict[str, object]:
    return next(check for check in checks if check["check_id"] == check_id)


def test_historical_validator_rejects_replace_raw_and_accepts_clean_provenance(
    tmp_path: Path,
) -> None:
    module = load_script(
        "75_validate_med_device_historical_snapshot_oos.py",
        "med_device_score_provenance_validator_test",
    )
    clean_path = tmp_path / "clean.csv"
    unsafe_path = tmp_path / "unsafe.csv"
    write_validator_csv(clean_path, validator_row(module, unsafe=False))
    write_validator_csv(unsafe_path, validator_row(module, unsafe=True))

    clean_checks: list[dict[str, object]] = []
    module.validate_daily_csv(
        clean_path,
        asof="2026-08-11",
        checks=clean_checks,
        new_daily_columns_required_from=date(2019, 1, 4),
        product_family_shadow_columns_required_from=None,
        score_provenance_columns_required_from=date(2026, 8, 11),
    )
    assert check_by_id(clean_checks, "daily_score_provenance_columns")["status"] == "PASS"
    assert check_by_id(clean_checks, "daily_score_provenance_consistency")["status"] == "PASS"
    assert check_by_id(clean_checks, "daily_no_ic_tilt_production_replacement")["status"] == "PASS"

    unsafe_checks: list[dict[str, object]] = []
    module.validate_daily_csv(
        unsafe_path,
        asof="2026-08-11",
        checks=unsafe_checks,
        new_daily_columns_required_from=date(2019, 1, 4),
        product_family_shadow_columns_required_from=None,
        score_provenance_columns_required_from=date(2026, 8, 11),
    )
    replacement_check = check_by_id(
        unsafe_checks,
        "daily_no_ic_tilt_production_replacement",
    )
    assert replacement_check["status"] == "FAIL"
    assert replacement_check["severity"] == "CRITICAL"
    assert replacement_check["observed"] == 1
    assert check_by_id(unsafe_checks, "daily_score_provenance_consistency")["status"] == "PASS"


def test_portfolio_adapter_excludes_only_unsafe_med_device_rows() -> None:
    cfg = {
        "model_family": "med_devices",
        "sector": "Healthcare",
        "industry": "Medical Devices",
        "industry_aggregate": "Medical Devices",
    }
    base = {
        "native_score_value": "60",
        "portfolio_candidate_score": "60",
        "composite_score": "60",
        "portfolio_candidate_gate": "1",
        "portfolio_candidate_reason": "ok",
        "calibration_eligible_flag": "1",
        "oos_score_valid_flag": "1",
        "calibration_sample_role": "strict_oos",
        "stage11_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_reason": "ok",
        "survivorship_corrected_panel_flag": "1",
        "source_snapshot_asof_date": "2026-08-11",
    }
    clean = {
        **base,
        "ticker": "CLEAN",
        "ic_tilted_composite_mode": "shadow",
        "production_score_source": "baseline_composite_score",
        "ic_tilt_applied_to_production_flag": "0",
    }
    unsafe = {
        **base,
        "ticker": "UNSAFE",
        "ic_tilted_composite_mode": "replace_raw",
        "production_score_source": "ic_tilted_composite_score",
        "ic_tilt_applied_to_production_flag": "1",
    }
    unsafe_flag_only = {
        **base,
        "ticker": "UNSAFE_FLAG",
        "ic_tilted_composite_mode": "shadow",
        "production_score_source": "baseline_composite_score",
        "ic_tilt_applied_to_production_flag": "1",
    }
    unsafe_source_only = {
        **base,
        "ticker": "UNSAFE_SOURCE",
        "ic_tilted_composite_mode": "shadow",
        "production_score_source": "ic_tilted_composite_score",
        "ic_tilt_applied_to_production_flag": "0",
    }


    adapted = {
        row.ticker: row
        for row in _adapt_med_devices(
            cfg,
            [clean, unsafe, unsafe_flag_only, unsafe_source_only],
        )
    }

    assert adapted["CLEAN"].investable_eligible == 1
    assert adapted["CLEAN"].calibration_research_eligible == 1
    assert adapted["CLEAN"].oos_score_valid_flag == 1
    for ticker in ("UNSAFE", "UNSAFE_FLAG", "UNSAFE_SOURCE"):
        assert adapted[ticker].investable_eligible == 0
        assert adapted[ticker].calibration_research_eligible == 0
        assert adapted[ticker].oos_score_valid_flag == 0
        assert adapted[ticker].eligibility_reason == "unsafe_production_score_provenance"
        assert (
            adapted[ticker].calibration_research_reason == "unsafe_production_score_provenance"
        )



def test_portfolio_adapter_requires_lineage_for_med_device_candidate() -> None:
    cfg = {
        "model_family": "med_devices",
        "sector": "Healthcare",
        "industry": "Medical Devices",
        "industry_aggregate": "Medical Devices",
        "_financial_lineage_policy_mode": "candidate_only",
    }
    base = {
        "ticker": "MDX",
        "asof_date": "2026-08-14",
        "native_score_value": "60",
        "portfolio_candidate_gate": "1",
        "portfolio_candidate_reason": "ok",
        "calibration_eligible_flag": "1",
        "oos_score_valid_flag": "1",
        "calibration_sample_role": "strict_oos",
        "stage11_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_reason": "ok",
        "survivorship_corrected_panel_flag": "1",
        "source_snapshot_asof_date": "2026-08-14",
        "ic_tilted_composite_mode": "shadow",
        "production_score_source": "baseline_composite_score",
        "ic_tilt_applied_to_production_flag": "0",
    }
    missing = _adapt_med_devices(cfg, [base])[0]
    assert missing.investable_eligible == 0
    assert missing.eligibility_reason == "financial_lineage:missing"

    incorporated = _adapt_med_devices(
        cfg,
        [
            {
                **base,
                "financial_lineage_checked_asof_date": "2026-08-14",
                "financial_lineage_status": "INCORPORATED",
                "financial_lineage_gate": "1",
                "financial_lineage_classification": "INCORPORATED",
                "latest_material_financial_filing_date": "2026-08-10",
                "latest_material_financial_form": "10-Q",
                "latest_material_financial_accession": "000012345624000001",
                "latest_material_financial_report_date": "2026-06-30",
                "incorporated_financial_filing_date": "2026-08-10",
                "incorporated_financial_accession": "000012345624000001",
                "incorporated_financial_report_date": "2026-06-30",
                "incorporated_financial_core_metric_count": "5",
                "financial_lineage_reason": "latest_sources_incorporated_before_scoring",
            }
        ],
    )[0]
    assert incorporated.investable_eligible == 1
    assert incorporated.financial_lineage_gate == 1
