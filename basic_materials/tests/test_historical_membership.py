"""Regression tests for governed Stage 2B historical reconciliation."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import FOUNDATION_SQL, connect, database_counts, init_db, migration_checksum, utc_now
from basic_materials.core.historical_membership import (
    HistoricalReconciliationValidationError,
    load_historical_reconciliation,
    load_historical_reconciliation_policy,
    read_and_validate_historical_reconciliation,
    validate_historical_reconciliation_database,
    validate_historical_reconciliation_manifest,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.universe import load_universe, load_universe_policy
from basic_materials.core.universe_validation import validate_universe_database


EXPECTED_COHORT_COUNTS = {
    "agricultural_inputs_crop_science": 3,
    "building_materials": 2,
    "commodity_chemicals": 2,
    "industrial_metals_mining": 3,
    "mining_royalty_streaming": 2,
    "precious_metals_producers": 3,
    "specialty_chemicals_materials": 1,
    "steel_producers_processors": 4,
}


def _contracts():
    config = load_config()
    policy = load_historical_reconciliation_policy(config.paths.historical_reconciliation_policy)
    manifest = validate_historical_reconciliation_manifest(
        config.paths.historical_reconciliation_manifest,
        policy,
        config.package_root,
    )
    bundle = read_and_validate_historical_reconciliation(
        policy=policy,
        manifest=manifest,
        candidate_policy_path=config.paths.historical_candidate_policy,
        candidate_manifest_path=config.paths.historical_candidate_manifest,
        candidate_path=config.paths.historical_candidates_csv,
    )
    return config, policy, manifest, bundle


def _loaded_current(tmp_path: Path):
    config, policy, historical_manifest, bundle = _contracts()
    current_manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    current_policy = load_universe_policy(config.paths.universe_policy)
    registry = load_source_registry(config.paths.source_registry)
    database_path = tmp_path / "basic_materials.sqlite"
    conn = connect(database_path, config.runtime.sqlite_timeout_seconds)
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(conn, registry, utc_now())
    conn.commit()
    load_universe(conn, policy=current_policy, manifest=current_manifest)
    return (
        conn,
        config,
        policy,
        historical_manifest,
        bundle,
        current_policy,
        current_manifest,
    )


def _manifest_with_modified_membership(manifest, path: Path):
    payload = path.read_bytes()
    entry = manifest.artifacts["historical_membership"]
    modified_entry = replace(
        entry,
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )
    artifacts = dict(manifest.artifacts)
    artifacts["historical_membership"] = modified_entry
    return replace(manifest, artifacts=artifacts)


def test_stage2b_contracts_are_exact_and_fail_closed() -> None:
    _, policy, manifest, bundle = _contracts()

    assert policy.expected_cohort_counts == EXPECTED_COHORT_COUNTS
    assert {name: entry.row_count for name, entry in manifest.artifacts.items()} == {
        "historical_membership": 20,
        "ticker_aliases": 4,
        "security_events": 22,
        "terminal_events": 20,
    }
    assert bundle.summary_dict()["cohort_counts"] == EXPECTED_COHORT_COUNTS
    assert {row["historical_ticker"] for row in bundle.historical_membership} == set(policy.expected_tickers)
    assert all(row["survivorship_corrected"] == "1" for row in bundle.historical_membership)
    assert all(row["calibration_eligible"] == "0" for row in bundle.historical_membership)
    assert all(row["survivorship_complete"] == "0" for row in bundle.terminal_events)
    assert all(row["calibration_eligible"] == "0" for row in bundle.terminal_events)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        (",1,0,1,0,1,Recent cash", ",1,0,1,1,1,Recent cash", "calibration_eligible must be 0"),
        ("X-202506,129729,0001163302", "X-202506,not_numeric,0001163302", "provider_asset_id"),
    ),
)
def test_stage2b_rejects_tampered_membership(tmp_path: Path, old: str, new: str, message: str) -> None:
    config, policy, manifest, _ = _contracts()
    tampered = tmp_path / "basic_materials_historical_membership.csv"
    source = config.paths.historical_membership_csv.read_text(encoding="utf-8")
    assert old in source
    tampered.write_text(source.replace(old, new, 1), encoding="utf-8")
    modified_manifest = _manifest_with_modified_membership(manifest, tampered)

    with pytest.raises(HistoricalReconciliationValidationError, match=message):
        read_and_validate_historical_reconciliation(
            policy=policy,
            manifest=modified_manifest,
            candidate_policy_path=config.paths.historical_candidate_policy,
            candidate_manifest_path=config.paths.historical_candidate_manifest,
            candidate_path=config.paths.historical_candidates_csv,
        )


def test_stage2b_loader_is_atomic_idempotent_and_preserves_current_validation(tmp_path: Path) -> None:
    conn, _, policy, manifest, bundle, current_policy, current_manifest = _loaded_current(tmp_path)
    try:
        first = load_historical_reconciliation(conn, policy=policy, manifest=manifest, bundle=bundle)
        second = load_historical_reconciliation(conn, policy=policy, manifest=manifest, bundle=bundle)
        counts = database_counts(conn)

        assert first.as_dict() == second.as_dict()
        assert counts["raw_source_payloads"] == 5
        assert counts["dim_company"] == 154
        assert counts["dim_security"] == 154
        assert counts["dim_identifier"] == 308
        assert counts["dim_ticker_alias"] == 4
        assert counts["dim_basic_materials_taxonomy"] == 154
        assert counts["dim_universe_membership"] == 154
        assert counts["fact_security_event"] == 22
        assert counts["fact_terminal_event_reconciliation"] == 20

        current_report = validate_universe_database(conn, policy=current_policy, manifest=current_manifest)
        assert current_report.passed, current_report.summary_dict()
        historical_report = validate_historical_reconciliation_database(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
            expected_current_rows=current_policy.expected_current_rows,
        )
        assert historical_report.passed, historical_report.summary_dict()
        assert historical_report.unresolved_terminal_events == 20
        assert historical_report.calibration_eligible_rows == 0
        assert [issue.issue_code for issue in historical_report.issues] == [
            "TERMINAL_RECONCILIATION_OPEN",
            "CALIBRATION_GATE_CLOSED",
        ]
    finally:
        conn.close()


def test_stage2b_loader_rolls_back_on_unresolved_canonical_security(tmp_path: Path) -> None:
    conn, _, policy, manifest, bundle, _, _ = _loaded_current(tmp_path)
    try:
        aliases = [dict(row) for row in bundle.ticker_aliases]
        aliases[0]["canonical_ticker"] = "ZZZZ"
        invalid_bundle = replace(bundle, ticker_aliases=tuple(aliases))
        with pytest.raises(HistoricalReconciliationValidationError, match="Canonical security is not loaded"):
            load_historical_reconciliation(conn, policy=policy, manifest=manifest, bundle=invalid_bundle)
        counts = database_counts(conn)
        assert counts["raw_source_payloads"] == 1
        assert counts["dim_company"] == 134
        assert counts["dim_ticker_alias"] == 0
        assert counts["fact_security_event"] == 0
    finally:
        conn.close()


def test_schema_v1_database_migrates_to_v3(tmp_path: Path) -> None:
    database_path = tmp_path / "basic_materials.sqlite"
    conn = connect(database_path)
    try:
        conn.executescript(FOUNDATION_SQL)
        now = utc_now()
        conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc) VALUES (1, ?, ?, ?)",
            ("basic_materials_foundation", migration_checksum(FOUNDATION_SQL), now),
        )
        conn.execute(
            """
            INSERT INTO sector_database_identity (
                identity_id, model_family, sector, schema_owner, schema_version, created_at_utc
            ) VALUES (1, 'basic_materials', 'Basic Materials', 'basic_materials', 1, ?)
            """,
            (now,),
        )
        conn.commit()

        result = init_db(conn)
        assert result["schema_version"] == 3
        assert result["migrations_applied"] == [2, 3]
        assert conn.execute("SELECT schema_version FROM sector_database_identity").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'dim_ticker_alias'"
        ).fetchone()[0] == 1
        assert "event_key" in {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(fact_security_event)").fetchall()
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'fact_adjusted_price_bar'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
