from __future__ import annotations

import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import yaml

from tests.biotech.conftest import load_script_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BIOTECH_ROOT = PROJECT_ROOT / "biotech_index"
DATA_ROOT = BIOTECH_ROOT / "data"


def read_rows(name: str) -> list[dict[str, str]]:
    with (DATA_ROOT / name).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_all_cohort_migrations_are_applied_once() -> None:
    migration_rows = read_rows("biotech_cohort_migration_20260831.csv")
    cohort_rows = read_rows("biotech_calibration_cohorts.csv")
    assert len(migration_rows) == 95
    assert len({row["ticker"] for row in migration_rows}) == 95
    assert len(cohort_rows) == len({row["ticker"] for row in cohort_rows}) == 536

    cohorts = {row["ticker"]: row for row in cohort_rows}
    for migration in migration_rows:
        applied = cohorts[migration["ticker"]]
        assert applied["biotech_calibration_cohort"] == migration["new_cohort"]
        assert f"prior_cohort={migration['expected_current_cohort']}" in applied["reason"]
        assert f"source={migration['reason']}" in applied["reason"]

    assert Counter(row["biotech_calibration_cohort"] for row in cohort_rows) == {
        "commercial_profitable_quality_or_mature": 70,
        "commercial_turnaround_or_unprofitable_growth": 96,
        "late_clinical_pivotal_or_registrational": 158,
        "platform_partnered_modality_pipeline": 93,
        "early_clinical_speculative_or_single_asset_pipeline": 119,
    }


def test_active_removals_are_effective_dated_and_keep_historical_cohorts() -> None:
    removal_rows = read_rows("biotech_active_universe_removals_20260831.csv")
    status_rows = read_rows("company_status_overrides.csv")
    cohort_tickers = {row["ticker"] for row in read_rows("biotech_calibration_cohorts.csv")}
    statuses = {row["ticker"]: row for row in status_rows}
    assert len(removal_rows) == 37
    assert len({row["ticker"] for row in removal_rows}) == 37

    company_master = load_script_module(
        "02_build_company_master.py", "company_master_cohort_reorganization_20260831"
    )
    for removal in removal_rows:
        ticker = removal["ticker"]
        effective_date = date.fromisoformat(removal["effective_date"])
        status = statuses[ticker]
        assert ticker in cohort_tickers
        assert status["decision"] == "remove"
        assert status["manual_exclude"] == "true"
        assert status["effective_date"] == removal["effective_date"]
        assert "removed_from_biotech_active_universe" in status["reason_codes"]
        assert company_master.status_override_is_effective(
            status, asof_date=(effective_date - timedelta(days=1)).isoformat()
        ) is False
        assert company_master.status_override_is_effective(
            status, asof_date=effective_date.isoformat()
        ) is True


def test_removed_names_are_excluded_from_all_historical_calibration() -> None:
    removal_tickers = {
        row["ticker"] for row in read_rows("biotech_active_universe_removals_20260831.csv")
    }
    with (BIOTECH_ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    assert set(config["calibration"]["exclude_tickers"]) == removal_tickers


def test_stage11_export_retains_but_excludes_removed_name() -> None:
    module = load_script_module(
        "56_generate_historical_biotech_score_csvs.py",
        "historical_export_calibration_exclusion",
    )

    class ExportModule:
        @staticmethod
        def enrich_portfolio_layer_contract_rows(
            rows: list[dict[str, object]], config: dict[str, object]
        ) -> None:
            del rows, config

    row = {
        "asof_date": "2024-01-05",
        "ticker": "TECH",
        "company_name": "Bio-Techne Corporation",
        "biotech_primary_cohort": "commercial_profitable_quality_or_mature",
        "biotech_cohort_investible_flag": 1.0,
        "biotech_cohort_calibration_eligible_flag": 1.0,
        "calibration_eligible_flag": 1.0,
        "opportunity_score": 62.0,
        "production_rank_score": 62.0,
        "core_structural_veto_flag": 0.0,
        "rank_quality_cap_vetoed": 0.0,
        "allocation_bucket": "positive",
    }
    exported = module.prepare_score_rows_for_export(
        [row],
        ExportModule(),
        config={},
        model_metadata={},
        market_context={
            "TECH": {
                "latest_price_date": "2024-01-05",
                "avg_dollar_volume_60d": 10_000_000.0,
            }
        },
        survivorship_corrected_panel=True,
        calibration_excluded_tickers={"TECH"},
    )[0]

    assert exported["ticker"] == "TECH"
    assert exported["native_score_value"] == 62.0
    assert exported["calibration_eligible_flag"] == 0.0
    assert exported["biotech_cohort_calibration_eligible_flag"] == 0.0
    assert exported["research_calibration_input_eligible_flag"] == 0.0
    assert exported["stage11_calibration_input_eligible_flag"] == 0.0
    assert exported["calibration_status_reason"] == module.CALIBRATION_EXCLUSION_REASON
    assert exported["research_calibration_reason"] == module.CALIBRATION_EXCLUSION_REASON
    assert exported["stage11_calibration_input_reason"] == module.CALIBRATION_EXCLUSION_REASON


def test_cohort_mapping_version_invalidates_old_calibration_cache() -> None:
    with (BIOTECH_ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    assert (
        config["biotech_scoring"]["calibration_cohorts"]["version"]
        == "calibration_cohort_v2_reorganized_20260831"
    )


def test_migration_applicator_is_idempotent_on_canonical_files() -> None:
    module = load_script_module(
        "65_apply_biotech_cohort_universe_migration.py",
        "cohort_universe_migration_idempotence",
    )
    _, cohort_rows = module.read_csv(module.DEFAULT_COHORT_REGISTRY, module.COHORT_FIELDS)
    _, status_rows = module.read_csv(module.DEFAULT_STATUS_OVERRIDES, module.STATUS_FIELDS)
    _, migration_rows = module.read_csv(
        module.DEFAULT_COHORT_MIGRATION,
        ("ticker", "expected_current_cohort", "new_cohort", "effective_date", "reason"),
    )
    _, removal_rows = module.read_csv(
        module.DEFAULT_ACTIVE_REMOVALS,
        ("ticker", "effective_date", "reason"),
    )
    assert module.apply_cohort_moves(
        cohort_rows,
        migration_rows,
        cohort_path=module.DEFAULT_COHORT_REGISTRY,
        migration_path=module.DEFAULT_COHORT_MIGRATION,
    ) == (0, 95)
    assert module.apply_active_removals(
        status_rows,
        removal_rows,
        status_path=module.DEFAULT_STATUS_OVERRIDES,
        removal_path=module.DEFAULT_ACTIVE_REMOVALS,
    ) == (0, 37)
