"""Regression tests for the independent Stage 4A financial contract."""

from __future__ import annotations

import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import connect, database_counts, init_db, utc_now
from basic_materials.core.financial_data_contract import (
    FinancialContractError,
    load_financial_contract,
    load_financial_data_policy,
    read_and_validate_financial_contract,
    validate_financial_contract_database,
    validate_financial_data_manifest,
    write_financial_contract_reports,
)
from basic_materials.core.historical_membership import (
    load_historical_reconciliation,
    load_historical_reconciliation_policy,
    read_and_validate_historical_reconciliation,
    validate_historical_reconciliation_manifest,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.reporting_profiles import (
    IssuerSeed,
    PayloadResult,
    build_reporting_profile,
)
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.universe import load_universe, load_universe_policy


EXPECTED_PROFILE_STATUS_COUNTS = {
    "annual_form_review_required": 2,
    "companyfacts_fallback_required": 6,
    "ready_for_ingestion": 140,
    "taxonomy_review_required": 6,
}


def _contracts():
    config = load_config()
    policy = load_financial_data_policy(config.paths.financial_data_policy)
    manifest = validate_financial_data_manifest(
        config.paths.financial_data_manifest,
        policy,
        config.package_root,
    )
    bundle = read_and_validate_financial_contract(
        policy=policy,
        manifest=manifest,
        universe_path=config.paths.universe_csv,
        historical_membership_path=config.paths.historical_membership_csv,
    )
    return config, policy, manifest, bundle


def _loaded_stage2(tmp_path: Path):
    config, policy, manifest, bundle = _contracts()
    current_policy = load_universe_policy(config.paths.universe_policy)
    current_manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    historical_policy = load_historical_reconciliation_policy(
        config.paths.historical_reconciliation_policy
    )
    historical_manifest = validate_historical_reconciliation_manifest(
        config.paths.historical_reconciliation_manifest,
        historical_policy,
        config.package_root,
    )
    historical_bundle = read_and_validate_historical_reconciliation(
        policy=historical_policy,
        manifest=historical_manifest,
        candidate_policy_path=config.paths.historical_candidate_policy,
        candidate_manifest_path=config.paths.historical_candidate_manifest,
        candidate_path=config.paths.historical_candidates_csv,
    )
    conn = connect(tmp_path / "basic_materials.sqlite")
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(
        conn,
        load_source_registry(config.paths.source_registry),
        utc_now(),
    )
    conn.commit()
    load_universe(conn, policy=current_policy, manifest=current_manifest)
    load_historical_reconciliation(
        conn,
        policy=historical_policy,
        manifest=historical_manifest,
        bundle=historical_bundle,
    )
    return conn, config, policy, manifest, bundle


def _payload_result(payload: dict, *, kind: str) -> PayloadResult:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return PayloadResult(
        payload=payload,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        url=f"https://data.sec.gov/{kind}/fixture.json",
        cache_path=Path(f"{kind}.json"),
        status="fixture",
    )


def _submissions(cik: str, rows: list[dict[str, str]]) -> dict:
    keys = (
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
    )
    return {
        "cik": int(cik),
        "name": "Fixture Materials Corp",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {key: [row[key] for row in rows] for key in keys},
            "files": [],
        },
    }


def test_financial_contract_is_exact_and_census_is_complete() -> None:
    _, policy, manifest, bundle = _contracts()
    assert policy.expected_total_profiles == 154
    assert policy.expected_metrics == 22
    assert len(bundle.profiles) == 154
    assert len(bundle.metrics) == 22
    assert not bundle.overrides
    assert manifest.artifacts["reporting_profiles"].sha256 == (
        "8d562825a53286e37fca486d36f295129e9f38597b4ce4216508817a64e16230"
    )
    assert {
        role: sum(row["role_type"] == role for row in bundle.profiles)
        for role in ("current_universe", "historical_pilot")
    } == {"current_universe": 134, "historical_pilot": 20}
    assert dict(
        sorted(
            {
                status: sum(row["profile_status"] == status for row in bundle.profiles)
                for status in {row["profile_status"] for row in bundle.profiles}
            }.items()
        )
    ) == EXPECTED_PROFILE_STATUS_COUNTS
    assert all(row["calibration_eligible"] == "0" for row in bundle.profiles)
    assert all(
        len(row["submissions_sha256"]) == 64
        for row in bundle.profiles
        if row["role_type"] == "current_universe"
    )


def test_profile_builder_is_acceptance_bounded_and_does_not_use_listing_currency() -> None:
    _, policy, _, _ = _contracts()
    cik = "0001234567"
    annual = {
        "accessionNumber": "0001234567-25-000001",
        "filingDate": "2025-02-20",
        "acceptanceDateTime": "2025-02-20T16:30:00Z",
        "reportDate": "2024-12-31",
        "form": "10-K",
        "primaryDocument": "annual.htm",
    }
    ordinary_6k = {
        "accessionNumber": "0001234567-25-000002",
        "filingDate": "2025-06-20",
        "acceptanceDateTime": "2025-06-20T10:00:00Z",
        "reportDate": "",
        "form": "6-K",
        "primaryDocument": "ordinary.htm",
    }
    future_quarter = {
        "accessionNumber": "0001234567-26-000003",
        "filingDate": "2026-10-01",
        "acceptanceDateTime": "2026-10-01T10:00:00Z",
        "reportDate": "2026-09-30",
        "form": "10-Q",
        "primaryDocument": "future.htm",
    }
    companyfacts = {
        "cik": int(cik),
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "accn": annual["accessionNumber"],
                                "form": "10-K",
                                "val": 100,
                            }
                        ]
                    }
                }
            }
        },
    }
    seed = IssuerSeed(
        profile_key="current:FIX",
        role_type="current_universe",
        ticker="FIX",
        cik=cik,
        company_name="Fixture Materials Corp",
        domicile_country="United States",
        trading_currency="USD",
        profile_asof_date="2026-09-05",
        source_cutoff_date="2026-09-05",
    )
    sources = policy.payload["sources"]
    profile = build_reporting_profile(
        seed,
        submissions=_payload_result(
            _submissions(cik, [future_quarter, ordinary_6k, annual]),
            kind="submissions",
        ),
        companyfacts=_payload_result(companyfacts, kind="companyfacts"),
        policy_version=policy.version,
        source_ids=sources,
        companyfacts_lag_days=120,
    )
    assert profile["primary_annual_form"] == "10-K"
    assert profile["latest_financial_accession"] == annual["accessionNumber"]
    assert profile["accounting_basis"] == "US_GAAP"
    assert profile["reporting_currency"] == "USD"
    assert profile["profile_status"] == "ready_for_ingestion"

    missing_facts = replace(
        _payload_result(companyfacts, kind="companyfacts"),
        payload=None,
        raw=None,
        sha256="",
        status="not_found",
    )
    unresolved = build_reporting_profile(
        seed,
        submissions=_payload_result(_submissions(cik, [annual]), kind="submissions"),
        companyfacts=missing_facts,
        policy_version=policy.version,
        source_ids=sources,
        companyfacts_lag_days=120,
    )
    assert unresolved["reporting_currency"] == ""
    assert unresolved["reporting_currency_method"] == "unresolved"
    assert unresolved["profile_status"] == "taxonomy_review_required"


def test_financial_contract_load_is_atomic_and_idempotent(tmp_path: Path) -> None:
    conn, _, policy, manifest, bundle = _loaded_stage2(tmp_path)
    try:
        first = load_financial_contract(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        second = load_financial_contract(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        counts = database_counts(conn)
        assert first.as_dict() == second.as_dict()
        assert first.profiles == 154
        assert first.metrics == 22
        assert counts["dim_issuer_reporting_profile"] == 154
        assert counts["dim_financial_metric"] == 22
        assert counts["bridge_financial_metric_concept"] == first.concept_links
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM dim_issuer_reporting_profile "
                "WHERE calibration_eligible <> 0"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_reporting_profile_row_tampering_is_rejected(tmp_path: Path) -> None:
    config, policy, manifest, _ = _contracts()
    source = manifest.artifacts["reporting_profiles"].path
    tampered = tmp_path / "basic_materials_reporting_profiles.csv"
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        rows = [dict(row) for row in reader]
    rows[0]["reporting_currency"] = "EUR"
    with tampered.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    payload = tampered.read_bytes()
    artifact = replace(
        manifest.artifacts["reporting_profiles"],
        path=tampered,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )
    modified_artifacts = dict(manifest.artifacts)
    modified_artifacts["reporting_profiles"] = artifact
    with pytest.raises(FinancialContractError, match="profile hash"):
        read_and_validate_financial_contract(
            policy=policy,
            manifest=replace(manifest, artifacts=modified_artifacts),
            universe_path=config.paths.universe_csv,
            historical_membership_path=config.paths.historical_membership_csv,
        )


def test_financial_contract_validator_and_reports_pass(tmp_path: Path) -> None:
    conn, _, policy, manifest, bundle = _loaded_stage2(tmp_path)
    try:
        load_financial_contract(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        report = validate_financial_contract_database(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        assert report.passed, report.summary_dict()
        assert report.actual_counts["profiles"] == 154
        assert report.profile_status_counts == EXPECTED_PROFILE_STATUS_COUNTS
        assert [issue.issue_code for issue in report.issues] == [
            "REPORTING_PROFILE_REVIEW_QUEUE_OPEN",
            "FINANCIAL_INGESTION_NOT_STARTED",
            "CALIBRATION_GATE_CLOSED",
        ]
        artifacts = write_financial_contract_reports(
            report,
            report_dir=tmp_path / "stage4a_reports",
        )
        assert set(artifacts) == {
            "summary",
            "issues",
            "profiles",
            "metrics",
            "census",
            "artifact_manifest",
        }
        assert all(Path(path).is_file() for path in artifacts.values())
    finally:
        conn.close()
