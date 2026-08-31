from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "med_devices" / "scripts"
REMOVED = {"VREX", "CATX", "VTAK", "NEOG", "XWEL", "MASS", "WRBY"}
EXPECTED_COHORTS = {
    "PROF": "capital_equipment_procedure_platforms",
    "MDAI": "emerging_single_product_medtech_platforms",
    "FEED": "emerging_single_product_medtech_platforms",
    "AVR": "emerging_single_product_medtech_platforms",
    "OWLT": "home_chronic_care_devices_dme_drug_delivery",
    "NVCR": "home_chronic_care_devices_dme_drug_delivery",
    "CNMD": "hospital_supplies_surgical_consumables_oem",
    "CBLL": "capital_equipment_procedure_platforms",
    "DCTH": "emerging_single_product_medtech_platforms",
    "MBOT": "emerging_single_product_medtech_platforms",
    "TLSI": "emerging_single_product_medtech_platforms",
    "IDXX": "diagnostics_clinical_tests",
}
EXPECTED_COUNTS = {
    "capital_equipment_procedure_platforms": 9,
    "diagnostics_clinical_tests": 27,
    "elective_vision_dental_aesthetic_devices": 10,
    "emerging_single_product_medtech_platforms": 9,
    "healthcare_services_cro_lab_services": 10,
    "home_chronic_care_devices_dme_drug_delivery": 16,
    "hospital_supplies_surgical_consumables_oem": 21,
    "implantable_interventional_devices_direct_payment": 15,
    "life_science_tools_research_instruments": 21,
    "orthopedics_spine_sports_implants": 12,
}


def load_script(filename: str, module_name: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "med_devices" / "config.yaml").read_text(encoding="utf-8"))


def test_final_active_universe_and_cohort_counts_are_exact() -> None:
    universe = load_script("01_load_med_device_universe.py", "med_universe_taxonomy_migration_test")
    taxonomy = load_script("22_build_med_device_calibration_cohorts.py", "med_taxonomy_migration_test")
    cfg = config()
    seed = ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv"
    action_path = ROOT / "med_devices" / "data" / "med_device_universe_actions.csv"
    override_path = ROOT / "med_devices" / "data" / "calibration_cohort_overrides.csv"

    companies = universe.parse_universe_rows(seed, config=cfg)
    actions = universe.load_universe_actions(action_path)
    companies, applied = universe.apply_universe_actions(companies, actions, asof=date(2026, 8, 30))
    active = [company for company in companies if company.is_active == 1]
    overrides = taxonomy.load_taxonomy_overrides(
        override_path,
        asof="2026-08-30",
        include_missing_pit_metadata=False,
    )

    assert len(companies) == 160
    assert len(active) == 150
    assert set(applied) == REMOVED
    assert all(next(company for company in companies if company.ticker == ticker).is_active == 0 for ticker in REMOVED)
    assert all(company.ticker in overrides for company in active)
    counts = Counter(overrides[company.ticker]["calibration_cohort"] for company in active)
    assert dict(sorted(counts.items())) == EXPECTED_COUNTS
    for ticker, cohort in EXPECTED_COHORTS.items():
        assert overrides[ticker]["calibration_cohort"] == cohort

    classifier_rows = []
    for company in active:
        cohort, confidence, source = taxonomy.classify_cohort(company.ticker, company.subsector)
        classifier_rows.append(
            {
                "ticker": company.ticker,
                "calibration_cohort": cohort,
                "taxonomy_confidence": confidence,
                "taxonomy_source": source,
                "analyst_reviewed": 0,
            }
        )
    assert taxonomy.fallback_ticker_heuristic_rows(classifier_rows) == []


def test_action_and_model_provenance_are_fail_closed() -> None:
    universe = load_script("01_load_med_device_universe.py", "med_universe_action_metadata_test")
    cfg = config()
    actions = universe.load_universe_actions(
        ROOT / "med_devices" / "data" / "med_device_universe_actions.csv"
    )
    assert set(actions) == REMOVED
    assert all(action.action == "exclude_all_history" for action in actions.values())
    assert all(action.reviewed_at == date(2026, 8, 30) for action in actions.values())
    assert all(action.reason and action.source_reference for action in actions.values())
    assert cfg["scoring"]["model_version"] == "med_device_score_v25_2026_08_taxonomy_rebuild"
    assert cfg["historical_backfill"]["strict_oos_start_date"] == "2026-08-31"
    profile = cfg["scoring"]["cohort_profiles"]["emerging_single_product_medtech_platforms"]
    assert profile["calibration_status"] == "excluded_from_tier1"


def test_backfill_runs_taxonomy_for_every_asof(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backfill = load_script("21_backfill_med_device_historical_scores.py", "med_taxonomy_backfill_test")
    commands: list[list[str]] = []
    monkeypatch.setattr(backfill, "run_command", lambda command: commands.append(command))

    assert backfill.DEFAULT_STAGES[0] == "taxonomy"
    assert "taxonomy" not in backfill.DEFAULT_SETUP_STAGES
    backfill.run_stage(
        stage="taxonomy",
        asof=date(2024, 1, 2),
        config_path=ROOT / "med_devices" / "config.yaml",
        db_path=tmp_path / "med.sqlite",
        include_historical_members=True,
    )
    assert len(commands) == 1
    assert "22_build_med_device_calibration_cohorts.py" in commands[0][1]
    assert "--asof" in commands[0]
    assert "2024-01-02" in commands[0]
    assert "--historical-panel" in commands[0]


def test_authoritative_historical_membership_cohort_is_not_reguessed() -> None:
    taxonomy = load_script("22_build_med_device_calibration_cohorts.py", "med_historical_cohort_test")
    cohort = "implantable_interventional_devices_direct_payment"
    assert taxonomy.classify_cohort("ABMD", cohort) == (
        cohort,
        0.98,
        "authoritative_subsector_cohort",
    )


def test_chunk_runner_creates_custom_log_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chunks = load_script("21b_run_med_device_daily_snapshot_chunks.py", "med_chunk_log_dir_test")
    monkeypatch.setattr(
        chunks.subprocess,
        "run",
        lambda *_args, **_kwargs: chunks.subprocess.CompletedProcess([], 0),
    )
    log_dir = tmp_path / "missing" / "logs"
    chunk = chunks.Chunk(
        start_asof="2024-01-02",
        end_asof="2024-01-02",
        asofs=("2024-01-02",),
    )
    returncode, log_path = chunks.run_chunk(
        chunk=chunk,
        chunk_index=1,
        total_chunks=1,
        config_path=ROOT / "med_devices" / "config.yaml",
        db_path=None,
        log_dir=log_dir,
        no_run_setup=True,
        force=True,
        attempt=1,
    )
    assert returncode == 0
    assert log_path.parent == log_dir
    assert log_path.exists()


def test_chunk_resume_requires_current_score_model_version(tmp_path: Path) -> None:
    chunks = load_script("21b_run_med_device_daily_snapshot_chunks.py", "med_chunk_resume_version_test")
    asof = "2024-01-02"
    output_dir = tmp_path / asof
    output_dir.mkdir(parents=True)
    header = "asof_date,ticker,score_model_version\n"
    for name in chunks.DEFAULT_REQUIRED_REVIEW_PACK_FILES:
        (output_dir / name).write_text(header + f"{asof},AAA,v24\n", encoding="utf-8")

    chunk = chunks.Chunk(start_asof=asof, end_asof=asof, asofs=(asof,))
    assert not chunks.chunk_complete(
        tmp_path,
        chunk,
        expected_score_model_version="v25",
    )
    (output_dir / "med_device_daily_composite_scores.csv").write_text(
        header + f"{asof},AAA,v25\n",
        encoding="utf-8",
    )
    assert chunks.chunk_complete(
        tmp_path,
        chunk,
        expected_score_model_version="v25",
    )


def test_full_range_oos_validation_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backfill = load_script("21_backfill_med_device_historical_scores.py", "med_oos_retry_test")
    calls: list[dict[str, Any]] = []

    def capture(_command: list[str], **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(backfill, "run_command", capture)
    backfill.run_oos_validation(
        config_path=ROOT / "med_devices" / "config.yaml",
        start_asof=date(2019, 1, 4),
        end_asof=date(2019, 1, 4),
        reports_root=tmp_path,
        output_csv=tmp_path / "strict.csv",
        diagnostic_output_csv=tmp_path / "diagnostic.csv",
        allow_missing_static_pit_metadata=False,
    )
    assert calls == [{"max_attempts": 1}]


def test_taxonomy_snapshot_replacement_and_history_are_point_in_time() -> None:
    taxonomy = load_script("22_build_med_device_calibration_cohorts.py", "med_taxonomy_storage_test")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    taxonomy.create_taxonomy_table(conn)
    taxonomy.create_taxonomy_history_table(conn)

    def row(company_id: int, ticker: str, cohort: str) -> dict[str, Any]:
        values: dict[str, Any] = {field: "" for field in taxonomy.FIELDNAMES}
        values.update(
            company_id=company_id,
            model_family="med_devices",
            ticker=ticker,
            company_name=ticker,
            calibration_cohort=cohort,
            capital_equipment_flag=0,
            consumables_flag=0,
            diagnostics_flag=0,
            implantable_flag=0,
            single_product_risk_flag=0,
            taxonomy_confidence=0.98,
            analyst_reviewed=1,
        )
        return values

    first = row(1, "AAA", "diagnostics_clinical_tests")
    stale = row(2, "STALE", "diagnostics_clinical_tests")
    taxonomy.upsert_rows(conn, [first, stale], replace_snapshot=True)
    taxonomy.replace_taxonomy_history_rows(conn, asof="2024-01-02", rows=[first, stale])
    second = row(1, "AAA", "capital_equipment_procedure_platforms")
    taxonomy.upsert_rows(conn, [second], replace_snapshot=True)
    taxonomy.replace_taxonomy_history_rows(conn, asof="2024-01-03", rows=[second])

    current = conn.execute("SELECT ticker, calibration_cohort FROM dim_company_model_taxonomy").fetchall()
    assert [(item["ticker"], item["calibration_cohort"]) for item in current] == [
        ("AAA", "capital_equipment_procedure_platforms")
    ]
    history = conn.execute(
        "SELECT asof_date, ticker, calibration_cohort FROM dim_company_model_taxonomy_history ORDER BY asof_date, ticker"
    ).fetchall()
    assert [(item["asof_date"], item["ticker"], item["calibration_cohort"]) for item in history] == [
        ("2024-01-02", "AAA", "diagnostics_clinical_tests"),
        ("2024-01-02", "STALE", "diagnostics_clinical_tests"),
        ("2024-01-03", "AAA", "capital_equipment_procedure_platforms"),
    ]


def test_calibration_requires_pit_taxonomy_and_handles_large_date_sets() -> None:
    calibration = load_script("23_backtest_med_device_cohort_neutral_scores.py", "med_taxonomy_calibration_test")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE dim_company(company_id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT)")
    conn.execute("INSERT INTO dim_company VALUES (1, 'AAA', 'Alpha')")
    with pytest.raises(RuntimeError, match="PIT taxonomy history is missing"):
        calibration.load_taxonomy(conn, asofs={"2024-01-02"})

    conn.execute(
        "CREATE TABLE dim_company_model_taxonomy_history("
        "asof_date TEXT, company_id INTEGER, ticker TEXT, calibration_cohort TEXT)"
    )
    conn.execute(
        "INSERT INTO dim_company_model_taxonomy_history VALUES ('2024-01-02', 1, 'AAA', 'diagnostics_clinical_tests')"
    )
    taxonomy = calibration.load_taxonomy(conn, asofs={"2024-01-02"})
    with pytest.raises(RuntimeError, match="does not cover every backtest row"):
        calibration.add_taxonomy_and_scores(
            [{"asof_date": "2024-01-03", "ticker": "AAA"}],
            taxonomy,
            {},
        )

    conn.execute("CREATE TABLE med_device_daily_scores(company_id INTEGER, asof_date TEXT, composite_score REAL)")
    conn.execute("CREATE TABLE feature_fda_product_risk(company_id INTEGER, asof_date TEXT)")
    conn.executemany(
        "INSERT INTO med_device_daily_scores VALUES (1, ?, ?)",
        [("2024-01-02", 51.0), ("2026-09-27", 52.0)],
    )
    asofs = {(date(2024, 1, 2) + timedelta(days=offset)).isoformat() for offset in range(1000)}
    scores = calibration.load_scores(conn, asofs=asofs)
    assert len(asofs) == 1000
    assert scores[("2024-01-02", "AAA")]["composite_score"] == 51.0
    assert scores[("2026-09-27", "AAA")]["composite_score"] == 52.0
