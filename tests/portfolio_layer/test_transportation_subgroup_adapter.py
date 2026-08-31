from __future__ import annotations

import csv
from pathlib import Path

import pytest

from industrials.core.config import load_yaml
from portfolio_layer.scores.adapters import (
    TRANSPORTATION_SUBGROUP_REQUIRED_COLUMNS,
    run_adapter,
)


ASOF = "2026-08-21"
RANK_REL = (
    Path("industrials")
    / "transportation"
    / "dashboard"
    / ASOF
    / "transportation_final_rank_table.csv"
)


def row(ticker: str, rank: int, score: float) -> dict[str, str]:
    value = {field: "" for field in TRANSPORTATION_SUBGROUP_REQUIRED_COLUMNS}
    value.update(
        {
            "asof_date": ASOF,
            "ticker": ticker,
            "company_name": f"{ticker} Corp",
            "sector": "Industrials",
            "industry": "Railroads",
            "industry_aggregate": "Transportation",
            "calibration_cohort": "surface",
            "final_score": str(score),
            "final_rank": str(rank),
            "rank_ready_flag": "1",
            "model_status": "complete",
            "score_confidence": "0.9",
            "score_model_version": "transportation_hierarchical_subgroup_v8_shadow",
            "model_version": "transportation_subgroup_model_v8_shadow",
            "scoring_contract_version": "transportation_final_rank_table_v2_subgroup_shadow",
            "portfolio_candidate_gate": "0",
            "portfolio_candidate_score": str(score),
            "portfolio_candidate_status": "shadow_only",
            "portfolio_candidate_reason": "shadow_subgroup_evidence_not_authorized",
            "calibration_eligible_flag": "1",
            "research_calibration_input_eligible_flag": "0",
            "research_calibration_reason": "current_snapshot_not_survivorship_corrected",
            "calibration_sample_role": "excluded",
            "stage11_calibration_panel_source": "current_transportation_subgroup_snapshot",
            "stage11_calibration_input_eligible_flag": "0",
            "stage11_calibration_input_reason": "current_snapshot_not_survivorship_corrected",
            "survivorship_corrected_panel_flag": "0",
            "oos_score_valid_flag": "0",
            "oos_score_asof_date": "",
            "oos_invalid_reason": "shadow_subgroup_future_evidence_not_available",
            "calibration_lock_date": "",
            "transportation_scoring_mode": "subgroup_v8",
            "transportation_production_state": "shadow",
            "transportation_group_recipe_version": "transportation_subgroup_v8_lock_v1",
            "transportation_subgroup_policy_sha256": "a" * 64,
            "transportation_cohort_id": "surface",
            "transportation_group_id": "rail",
            "transportation_group_recipe_key": "surface::rail",
            "transportation_group_recipe_sha256": "b" * 64,
            "transportation_group_ranking_mode": "ranked",
            "transportation_group_aggregate_weight": "1",
            "transportation_group_rank": str(rank),
            "transportation_membership_scope": "current_recipe",
            "transportation_membership_effective_from": "2019-01-02",
            "transportation_membership_effective_to": "",
            "transportation_component_recipe_state": "active",
            "transportation_applied_component_weights_sha256": "c" * 64,
            "transportation_subgroup_score_sha256": "d" * 64,
            "transportation_expected_group_count": "1",
            "transportation_expected_ticker_count": "2",
            "transportation_group_expected_ticker_count": "2",
            "transportation_production_lock_id": "",
            "transportation_decision_manifest_sha256": "",
        }
    )
    return value


def cfg() -> dict[str, object]:
    return {
        "model_family": "transportation",
        "adapter": "transportation_subgroup",
        "file_mode": "dated",
        "file_path": str(RANK_REL).replace("\\", "/").replace(
            ASOF, "{yyyy-mm-dd}"
        ),
        "sector": "Industrials",
        "industry": "Transportation",
        "industry_aggregate": "Transportation",
        "require_oos_score_valid": True,
        "calibration": {
            "neutral": "median",
            "scale": 50.0,
            "expected_alpha_at_full": 0.0,
        },
    }


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({field for item in rows for field in item})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_transportation_subgroup_adapter_consumes_shadow_as_noninvestable(
    tmp_path: Path,
) -> None:
    write_rows(
        tmp_path / RANK_REL,
        [row("AAA", 1, 90.0), row("BBB", 2, 10.0)],
    )

    result = run_adapter(cfg(), tmp_path, ASOF)

    assert result.adapter == "transportation_subgroup"
    assert [item.ticker for item in result.rows] == ["AAA", "BBB"]
    assert {item.investable_eligible for item in result.rows} == {0}
    assert {item.oos_score_valid_flag for item in result.rows} == {0}


def test_transportation_subgroup_adapter_rejects_missing_group_rows(
    tmp_path: Path,
) -> None:
    write_rows(tmp_path / RANK_REL, [row("AAA", 1, 90.0)])
    with pytest.raises(ValueError, match="ticker census"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_ambiguous_recipe(
    tmp_path: Path,
) -> None:
    second = row("BBB", 2, 10.0)
    second["transportation_group_recipe_sha256"] = "e" * 64
    write_rows(tmp_path / RANK_REL, [row("AAA", 1, 90.0), second])
    with pytest.raises(ValueError, match="ambiguous subgroup recipe lineage"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_shadow_gate(
    tmp_path: Path,
) -> None:
    first = row("AAA", 1, 90.0)
    first["portfolio_candidate_gate"] = "1"
    write_rows(tmp_path / RANK_REL, [first, row("BBB", 2, 10.0)])
    with pytest.raises(ValueError, match="cannot assert portfolio/OOS gates"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_latent_shadow_alpha(
    tmp_path: Path,
) -> None:
    write_rows(
        tmp_path / RANK_REL,
        [row("AAA", 1, 90.0), row("BBB", 2, 10.0)],
    )
    bad_cfg = cfg()
    bad_cfg["calibration"] = {
        "neutral": "median",
        "scale": 50.0,
        "expected_alpha_at_full": 0.15,
    }
    with pytest.raises(ValueError, match="expected_alpha_at_full=0"):
        run_adapter(bad_cfg, tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_shadow_research_gate(
    tmp_path: Path,
) -> None:
    first = row("AAA", 1, 90.0)
    first["stage11_calibration_input_eligible_flag"] = "1"
    write_rows(tmp_path / RANK_REL, [first, row("BBB", 2, 10.0)])
    with pytest.raises(ValueError, match="cannot assert research gates"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_shadow_survivorship_claim(
    tmp_path: Path,
) -> None:
    first = row("AAA", 1, 90.0)
    first["survivorship_corrected_panel_flag"] = "1"
    write_rows(tmp_path / RANK_REL, [first, row("BBB", 2, 10.0)])
    with pytest.raises(ValueError, match="cannot assert research gates"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_legacy_shadow_cannot_import_stage11_sidecar_evidence(
    tmp_path: Path,
) -> None:
    rows = [row("AAA", 1, 90.0), row("BBB", 2, 10.0)]
    for item in rows:
        item["transportation_scoring_mode"] = ""
    rows[0]["stage11_calibration_input_eligible_flag"] = "1"
    rows[0]["survivorship_corrected_panel_flag"] = "1"
    write_rows(tmp_path / RANK_REL, rows)

    with pytest.raises(
        ValueError,
        match="legacy Transportation shadow row cannot assert research lineage",
    ):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_out_of_period_membership(
    tmp_path: Path,
) -> None:
    first = row("AAA", 1, 90.0)
    first["transportation_membership_effective_to"] = "2025-12-31"
    write_rows(tmp_path / RANK_REL, [first, row("BBB", 2, 10.0)])
    with pytest.raises(ValueError, match="does not cover score date"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_unsealed_promoted_state(
    tmp_path: Path,
) -> None:
    rows = [row("AAA", 1, 90.0), row("BBB", 2, 10.0)]
    for item in rows:
        item["transportation_production_state"] = "promoted"
    write_rows(tmp_path / RANK_REL, rows)
    with pytest.raises(ValueError, match="lacks lock/decision lineage"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_rejects_syntactically_sealed_promotion(
    tmp_path: Path,
) -> None:
    rows = [row("AAA", 1, 90.0), row("BBB", 2, 10.0)]
    for item in rows:
        item["transportation_production_state"] = "promoted"
        item["transportation_production_lock_id"] = "syntactic_lock_only"
        item["transportation_decision_manifest_sha256"] = "e" * 64
    write_rows(tmp_path / RANK_REL, rows)
    with pytest.raises(ValueError, match="hash-bound lock/decision verification"):
        run_adapter(cfg(), tmp_path, ASOF)


def test_transportation_subgroup_adapter_accepts_bounded_diagnostic_shadow(
    tmp_path: Path,
) -> None:
    rows = [row("AAA", 1, 90.0), row("BBB", 2, 10.0)]
    for item in rows:
        item["transportation_membership_scope"] = (
            "pre_effective_policy_diagnostic_replay"
        )
        item["transportation_membership_effective_from"] = ASOF
        item["transportation_membership_effective_to"] = ASOF
    write_rows(tmp_path / RANK_REL, rows)
    result = run_adapter(cfg(), tmp_path, ASOF)
    assert len(result.rows) == 2
    assert not any(item.investable_eligible for item in result.rows)


def test_transportation_portfolio_configuration_remains_disabled_and_zero_cap() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_yaml(root / "portfolio_layer" / "config.yaml")
    source = next(
        item
        for item in config["score_contract"]["sectors"]
        if item["model_family"] == "transportation"
    )
    assert source["adapter"] == "transportation_subgroup"
    assert source["enabled"] is False
    assert source["required"] is False
    assert source["calibration"]["expected_alpha_at_full"] == 0.0
    assert config["optimizer"]["sector_weight_caps"]["transportation"] == 0.0
    assert (
        config["black_litterman_fusion"]["strategic_sector_weights"][
            "transportation"
        ]
        == 0.0
    )


def test_transportation_cannot_bypass_governance_via_generic_adapter(
    tmp_path: Path,
) -> None:
    bad_cfg = cfg()
    bad_cfg['adapter'] = 'industrial_family'

    with pytest.raises(ValueError, match='bypass promotion governance'):
        run_adapter(bad_cfg, tmp_path, ASOF)
