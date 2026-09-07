"""F0 closure workbench and fixture-first parser contract regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import connect, init_db, utc_now
from basic_materials.core.f0_closure import (
    build_f0_closure_workbench,
    load_f0_closure_policy,
    write_f0_closure_workbench,
)
from basic_materials.core.historical_pit_preflight import (
    PitPreflightError,
    load_historical_pit_preflight_policy,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.specialized_contract import (
    load_specialized_contract,
    load_specialized_metric_registry,
    load_specialized_source_policy,
)
from basic_materials.core.specialized_parser_contract import (
    SpecializedParserContractError,
    load_specialized_parser_fixtures,
    load_specialized_parser_policy,
    next_parse_action,
    validate_specialized_parser_contract,
    write_specialized_parser_contract_reports,
)
from basic_materials.core.universe import load_universe, load_universe_policy


def _fixture_database(tmp_path: Path):
    config = load_config()
    database = tmp_path / "basic_materials.sqlite"
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
    conn.commit()
    manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    load_universe(
        conn,
        policy=load_universe_policy(config.paths.universe_policy),
        manifest=manifest,
    )
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    load_specialized_contract(conn, registry=registry, source_policy=source_policy)
    conn.close()
    return config, database, registry, source_policy


def test_f0_workbench_is_query_only_and_deduplicates_acquisition_units(tmp_path: Path) -> None:
    config, database, registry, source_policy = _fixture_database(tmp_path)
    before = database.stat()
    conn = connect(database, config.runtime.sqlite_timeout_seconds, read_only=True)
    try:
        policy = load_f0_closure_policy(config.paths.f0_closure_policy)
        pit_policy = load_historical_pit_preflight_policy(config.paths.historical_pit_preflight_policy)
        report = build_f0_closure_workbench(
            conn,
            database_path=database,
            policy=policy,
            pit_policy=pit_policy,
            registry=registry,
            source_policy=source_policy,
            historical_candidate_policy_path=config.paths.historical_candidate_policy,
            historical_candidate_manifest_path=config.paths.historical_candidate_manifest,
            historical_candidates_path=config.paths.historical_candidates_csv,
        )
        assert report.structurally_valid is True
        assert report.database_unchanged is True
        assert report.f0_closure_ready is False
        assert report.counts["historical_candidate_decisions_open"] == 72
        assert report.counts["current_membership_histories_open"] == 134
        assert report.counts["specialized_applicability_reviews_open"] > 0
        assert report.counts["specialized_source_rows_open"] > report.counts["source_acquisition_units"]
        assert all(row["decision"] == "" for row in report.candidate_rows)
        assert all(row["approved_membership_start_date"] == "" for row in report.membership_rows)
        assert all(row["execution_status"] == "blocked_until_f0_contract_seals" for row in report.acquisition_rows)
        artifacts = write_f0_closure_workbench(report, report_dir=tmp_path / "workbench")
        assert all(Path(path).is_file() for path in artifacts.values())
    finally:
        conn.close()
    after = database.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_parser_contract_covers_every_table_family_and_fixture_case(tmp_path: Path) -> None:
    config = load_config()
    policy = load_specialized_parser_policy(config.paths.specialized_parser_policy)
    fixtures = load_specialized_parser_fixtures(config.paths.specialized_parser_fixtures)
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    report = validate_specialized_parser_contract(
        policy=policy,
        fixtures=fixtures,
        registry=registry,
        source_policy=source_policy,
    )
    assert report.contract_valid is True
    assert report.fixture_gate_passed is True
    assert report.production_execution_allowed is False
    assert report.counts["metrics"] == 64
    assert report.counts["core_metrics"] == 40
    assert report.counts["expected_table_families"] == report.counts["assigned_table_families"]
    assert report.counts["fixture_cases"] == 8
    assert report.counts["fixture_failures"] == 0
    assert report.counts["maximum_physical_parse_passes_per_contract_version"] == 2
    artifacts = write_specialized_parser_contract_reports(report, report_dir=tmp_path / "parser")
    assert all(Path(path).is_file() for path in artifacts.values())


def test_parse_scheduler_requires_changed_evidence_for_physical_reparse() -> None:
    assert next_parse_action(full_passes=0, residual_passes=0, unresolved_pairs=100) == "full_pass"
    assert next_parse_action(full_passes=1, residual_passes=0, unresolved_pairs=0) == "complete"
    assert next_parse_action(full_passes=1, residual_passes=0, unresolved_pairs=10) == "policy_review_only"
    assert (
        next_parse_action(full_passes=1, residual_passes=0, unresolved_pairs=10, new_source_hashes=2)
        == "residual_pass"
    )
    assert (
        next_parse_action(
            full_passes=1,
            residual_passes=1,
            unresolved_pairs=10,
            parser_rule_version_changed=True,
        )
        == "new_contract_version_required"
    )
    with pytest.raises(SpecializedParserContractError):
        next_parse_action(full_passes=2, residual_passes=0, unresolved_pairs=10)


def test_pit_policy_rejects_noncontiguous_chronological_contract(tmp_path: Path) -> None:
    config = load_config()
    malformed = tmp_path / "bad_pit_policy.yaml"
    text = config.paths.historical_pit_preflight_policy.read_text(encoding="utf-8")
    malformed.write_text(text.replace("start_date: '2021-01-01'", "start_date: '2021-01-02'"), encoding="utf-8")
    with pytest.raises(PitPreflightError, match="contiguous"):
        load_historical_pit_preflight_policy(malformed)
