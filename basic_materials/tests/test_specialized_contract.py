"""Stage 4D/5A regression tests for the extraction-first Basic Materials contract."""

from __future__ import annotations

from pathlib import Path

from basic_materials.core.config import load_config
from basic_materials.core.db import connect, init_db, utc_now
from basic_materials.core.historical_pit_preflight import (
    load_historical_pit_preflight_policy,
    run_historical_pit_preflight,
    write_historical_pit_preflight_reports,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.specialized_contract import (
    SOURCE_FAMILIES,
    load_specialized_contract,
    load_specialized_metric_registry,
    load_specialized_source_policy,
    validate_specialized_contract_database,
    write_specialized_contract_reports,
)
from basic_materials.core.universe import load_universe, load_universe_policy


def _loaded_contract(tmp_path: Path):
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
    stats = load_specialized_contract(conn, registry=registry, source_policy=source_policy)
    return config, database, conn, registry, source_policy, stats


def test_specialized_registry_is_complete_and_zero_weight() -> None:
    config = load_config()
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)

    assert len(registry.metrics) == 64
    assert len({metric.cohort_id for metric in registry.metrics}) == 8
    assert sum(len(metric.operands) for metric in registry.metrics) == 16
    assert source_policy.target_history_start_date == "2019-01-01"
    assert source_policy.source_census_start_date == "2017-01-01"
    assert set(source_policy.source_family_priority) == set(SOURCE_FAMILIES)


def test_specialized_contract_load_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    config, _, conn, registry, source_policy, first = _loaded_contract(tmp_path)
    try:
        second = load_specialized_contract(conn, registry=registry, source_policy=source_policy)
        assert first.metrics == second.metrics == 64
        assert first.operand_links == second.operand_links == 16
        assert first.applicability_rows == second.applicability_rows == 134 * 64
        assert first.source_census_rows == second.source_census_rows

        report = validate_specialized_contract_database(
            conn,
            registry=registry,
            source_policy=source_policy,
        )
        assert report.contract_valid is True
        assert report.stage5a_sealed is False
        assert report.counts["source_family_identity_pairs_accounted"] == 134 * len(SOURCE_FAMILIES)
        assert report.counts["open_applicability_reviews"] > 0
        assert report.counts["open_source_rows"] > 0
        unsafe = conn.execute(
            """
            SELECT COUNT(*) FROM dim_specialized_metric
            WHERE production_weight <> 0 OR scoring_eligible <> 0 OR calibration_eligible <> 0
            """
        ).fetchone()[0]
        assert unsafe == 0
        artifacts = write_specialized_contract_reports(report, report_dir=tmp_path / "stage5a")
        assert all(Path(path).is_file() for path in artifacts.values())
        assert config.model.portfolio_candidate_gate is False
    finally:
        conn.close()


def test_stage4d_preflight_is_query_only_and_does_not_materialize_history(tmp_path: Path) -> None:
    config, database, writable, registry, source_policy, _ = _loaded_contract(tmp_path)
    writable.close()
    before = database.stat()
    conn = connect(database, config.runtime.sqlite_timeout_seconds, read_only=True)
    try:
        policy = load_historical_pit_preflight_policy(config.paths.historical_pit_preflight_policy)
        report = run_historical_pit_preflight(
            conn,
            database_path=database,
            policy=policy,
            registry=registry,
            source_policy=source_policy,
            historical_candidate_policy_path=config.paths.historical_candidate_policy,
            historical_candidate_manifest_path=config.paths.historical_candidate_manifest,
            historical_candidates_path=config.paths.historical_candidates_csv,
        )
        assert report.database_unchanged is True
        assert report.feasibility_passed is False
        assert report.pit_materialization_allowed is False
        codes = {row["issue_code"] for row in report.blockers}
        assert "HISTORICAL_CANDIDATE_DECISIONS_OPEN" in codes
        assert "CURRENT_MEMBERSHIP_HISTORY_UNRECONSTRUCTED" in codes
        assert "SPECIALIZED_SOURCE_CLOSURE_INCOMPLETE" in codes
        assert "SPECIALIZED_PARSER_PLAN_INCOMPLETE" in codes
        artifacts = write_historical_pit_preflight_reports(report, report_dir=tmp_path / "stage4d")
        assert all(Path(path).is_file() for path in artifacts.values())
    finally:
        conn.close()
    after = database.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert not any(path.name.startswith(("feature_", "score_", "rank_", "outcome_")) for path in tmp_path.iterdir())
