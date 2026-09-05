"""Regression tests for the Basic Materials foundation and current universe."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import DatabaseIdentityError, connect, database_counts, init_db, utc_now
from basic_materials.core.independence import run_independence_checks
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.universe import (
    UniverseValidationError,
    load_universe,
    load_universe_policy,
    read_and_validate_universe,
)
from basic_materials.core.universe_validation import validate_universe_database, write_validation_reports


EXPECTED_COHORT_COUNTS = {
    "agricultural_inputs_crop_science": 11,
    "building_materials": 12,
    "commodity_chemicals": 12,
    "industrial_metals_mining": 11,
    "mining_royalty_streaming": 10,
    "precious_metals_producers": 30,
    "specialty_chemicals_materials": 37,
    "steel_producers_processors": 11,
}


def _contracts():
    config = load_config()
    manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    policy = load_universe_policy(config.paths.universe_policy)
    registry = load_source_registry(config.paths.source_registry)
    return config, manifest, policy, registry


def _loaded_connection(tmp_path: Path):
    config, manifest, policy, registry = _contracts()
    database_path = tmp_path / "basic_materials.sqlite"
    conn = connect(database_path, config.runtime.sqlite_timeout_seconds)
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(conn, registry, utc_now())
    conn.commit()
    stats = load_universe(conn, policy=policy, manifest=manifest)
    return conn, database_path, manifest, policy, stats


def test_config_manifest_policy_and_source_rows_are_exact() -> None:
    config, manifest, policy, _ = _contracts()
    rows = read_and_validate_universe(config.paths.universe_csv, policy)

    assert manifest.sha256 == "8fe31311a7683e9b207171ace0fe89156fac6154c0a4b40b11c73ed9b9e11be9"
    assert manifest.row_count == 134
    assert len(rows) == 134
    assert len({row.ticker for row in rows}) == 134
    assert len({row.cik for row in rows}) == 134
    assert policy.cohort_counts() == EXPECTED_COHORT_COUNTS
    assert sum(row.country != "United States" for row in rows) == 51
    assert all(row.calibration_group == row.subsector for row in rows)
    assert sum(row.calibration_group_derived for row in rows) == 134


def test_independence_gate_passes() -> None:
    config = load_config()
    report = run_independence_checks(config)
    assert report.passed, report.as_dict()
    assert all(check.passed for check in report.checks)


def test_unidentified_nonempty_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "basic_materials.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE foreign_sector_data (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(DatabaseIdentityError, match="non-empty database"):
            init_db(conn)
    finally:
        conn.close()


def test_loader_is_atomic_idempotent_and_populates_derived_group(tmp_path: Path) -> None:
    conn, _, manifest, policy, first_stats = _loaded_connection(tmp_path)
    try:
        second_stats = load_universe(conn, policy=policy, manifest=manifest)
        counts = database_counts(conn)
        assert first_stats.rows_loaded == second_stats.rows_loaded == 134
        assert first_stats.calibration_groups_derived == 134
        assert counts["raw_source_payloads"] == 1
        assert counts["dim_company"] == 134
        assert counts["dim_security"] == 134
        assert counts["dim_identifier"] == 268
        assert counts["dim_basic_materials_taxonomy"] == 134
        assert counts["dim_universe_membership"] == 134
        assert dict(first_stats.cohort_counts) == EXPECTED_COHORT_COUNTS
        mismatches = conn.execute(
            "SELECT COUNT(*) FROM dim_basic_materials_taxonomy WHERE calibration_group <> cohort_id"
        ).fetchone()[0]
        assert mismatches == 0
        unsafe_memberships = conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE current_source_only <> 1 OR survivorship_corrected <> 0 OR calibration_eligible <> 0
            """
        ).fetchone()[0]
        assert unsafe_memberships == 0
    finally:
        conn.close()


def test_modified_universe_is_rejected_before_load(tmp_path: Path) -> None:
    config, _, policy, _ = _contracts()
    modified = tmp_path / "basic_materials.csv"
    text = config.paths.universe_csv.read_text(encoding="utf-8")
    modified.write_text(text.replace(",Basic Materials,", ",Technology,", 1), encoding="utf-8")
    with pytest.raises(UniverseValidationError, match="sector must be"):
        read_and_validate_universe(modified, policy)


def test_database_validation_and_reports_pass_with_expected_warning(tmp_path: Path) -> None:
    conn, _, manifest, policy, _ = _loaded_connection(tmp_path)
    try:
        report = validate_universe_database(conn, policy=policy, manifest=manifest)
        assert report.passed, report.summary_dict()
        assert report.actual_rows == 134
        assert report.actual_cohort_counts == EXPECTED_COHORT_COUNTS
        assert [issue.issue_code for issue in report.issues] == ["CURRENT_UNIVERSE_NOT_PIT"]

        artifacts = write_validation_reports(report, policy=policy, report_dir=tmp_path / "reports")
        assert set(artifacts) == {
            "summary",
            "issues",
            "universe",
            "cohort_census",
            "artifact_manifest",
        }
        assert all(Path(path).is_file() for path in artifacts.values())
    finally:
        conn.close()
