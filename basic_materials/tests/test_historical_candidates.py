"""Regression tests for the Stage 2B historical candidate review queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from basic_materials.core.historical_candidates import (
    HistoricalCandidateValidationError,
    load_historical_candidate_policy,
    read_and_validate_historical_candidates,
    summarize_historical_candidates,
    validate_historical_candidate_manifest,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PACKAGE_ROOT / "data" / "basic_materials_historical_candidate_policy.yaml"
MANIFEST_PATH = PACKAGE_ROOT / "data" / "basic_materials_historical_candidate_manifest.yaml"
CANDIDATE_PATH = PACKAGE_ROOT / "system_csvs" / "basic_materials_deactivated_candidates.csv"

EXPECTED_COUNTS = {
    "agricultural_inputs_crop_science": 9,
    "building_materials": 10,
    "commodity_chemicals": 7,
    "industrial_metals_mining": 11,
    "mining_royalty_streaming": 4,
    "precious_metals_producers": 13,
    "specialty_chemicals_materials": 10,
    "steel_producers_processors": 8,
}


def test_candidate_census_is_complete_and_fail_closed() -> None:
    policy = load_historical_candidate_policy(POLICY_PATH)
    manifest = validate_historical_candidate_manifest(MANIFEST_PATH, CANDIDATE_PATH)
    rows = read_and_validate_historical_candidates(CANDIDATE_PATH, policy)
    summary = summarize_historical_candidates(rows, policy)

    assert len(rows) == 72
    assert manifest["sha256"] == "eac142b0426c7bc16ee273898f45f0f14db25f1daa8e67b6cf93362a7026f711"
    assert manifest["row_count"] == 72
    assert summary.cohort_counts == EXPECTED_COUNTS
    assert summary.event_source_rows >= 16
    assert summary.provider_mapping_blocked_rows == 1
    assert summary.include_in_historical_universe_rows == 0
    assert summary.calibration_eligible_rows == 0
    assert {row.review_status for row in rows} == {"candidate_unapproved"}
    assert {row.historical_ticker for row in rows} >= {"ANV", "BIOA", "GOLD", "MCP", "NSR", "X"}


def test_candidate_census_rejects_premature_calibration_activation(tmp_path: Path) -> None:
    policy = load_historical_candidate_policy(POLICY_PATH)
    tampered = tmp_path / "candidates.csv"
    source = CANDIDATE_PATH.read_text(encoding="utf-8")
    tampered.write_text(source.replace(",0,0,Norgate", ",0,1,Norgate", 1), encoding="utf-8")

    with pytest.raises(HistoricalCandidateValidationError, match="calibration_eligible must be 0"):
        read_and_validate_historical_candidates(tampered, policy)
