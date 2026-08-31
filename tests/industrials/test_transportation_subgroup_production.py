from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from industrials.transportation.contracts import SCORING_FEATURE_FIELDS
from industrials.transportation.subgroup_production_lock import (
    build_subgroup_lock_payload,
    validate_subgroup_lock_payload,
)
from industrials.transportation.subgroup_production_scoring import (
    build_shadow_subgroup_rank_rows,
)


POLICY_SHA256 = "a" * 64


def load_shadow_publisher():
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "industrials"
        / "transportation"
        / "scripts"
        / "44_publish_transportation_v8_subgroup_shadow.py"
    )
    spec = importlib.util.spec_from_file_location(
        "transportation_subgroup_shadow_publisher_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy() -> dict[str, object]:
    active = {
        "market_trend": 0.0,
        "quality": 0.0,
        "growth": 0.0,
        "valuation": 0.0,
        "operating_efficiency": 0.0,
        "capital_risk": 0.0,
        "positioning": 0.0,
        "specialized": 1.0,
    }
    fallback = dict(active)
    fallback["specialized"] = 0.0
    fallback["market_trend"] = 1.0
    return {
        "effective_from": "2026-08-21",
        "cohorts": {
            "surface": {
                "aggregate_group_weights": {"rail": 1.0},
                "groups": {
                    "rail": {
                        "tickers": ["AAA", "BBB"],
                        "ranking_mode": "ranked",
                        "minimum_cross_section": 2,
                        "minimum_specialized_breadth": 2,
                        "specialized_activation": "required_for_calibration",
                        "component_weights_active": active,
                        "component_weights_fallback": fallback,
                        "specialized_pack": {
                            "operating_ratio_yoy_improvement": {
                                "weight": 1.0,
                                "source_metric": "operating_ratio",
                                "transform": "yoy_improvement",
                                "direction": 1,
                            }
                        },
                    }
                },
            }
        },
        "historical_calibration_only": {
            "OLD": {
                "cohort": "surface",
                "group": "rail",
                "effective_from": "2019-01-02",
                "effective_to": "2020-12-31",
            }
        },
        "aggregation": {
            "selection_fraction": 0.20,
            "missing_group_policy": "fail_closed_no_cross_group_weight_redistribution",
        },
    }


def source_row(ticker: str, *, asof: str = "2026-08-21") -> dict[str, str]:
    row = {field: "" for field in SCORING_FEATURE_FIELDS}
    row.update(
        {
            "asof_date": asof,
            "ticker": ticker,
            "company_name": f"{ticker} Corp",
            "sector": "Industrials",
            "industry": "Railroads",
            "industry_aggregate": "Transportation",
            "subsector": "Transportation",
            "calibration_cohort": "surface",
            "calibration_cohort_name": "Surface",
            "calibration_use": "core",
            "development_stage": "operating",
            "classification_policy_version": "test",
            "calibration_pool": "surface_freight_and_logistics",
            "economic_peer_group": "rail",
            "risk_tier": "operating",
            "portfolio_role": "core_candidate",
            "membership_source_id": "fixture",
            "membership_basis": "point_in_time",
            "membership_start_date": "2019-01-02",
            "membership_end_date": "",
            "membership_status": "active",
            "membership_confidence": "1",
            "metric_registry_version": "fixture",
            "metric_values_json": "{}",
            "metric_status_json": "{}",
            "component_coverage_json": "{}",
            "applicable_metric_count": "0",
            "observed_metric_count": "0",
            "required_metric_count": "0",
            "required_metric_observed_count": "0",
            "specialized_metric_count": "0",
            "specialized_metric_observed_count": "0",
            "specialized_coverage": "0",
            "rank_ready_policy": "eligible_fixture",
            "minimum_financial_confidence": "0",
            "policy_valid_from": "2019-01-02",
            "policy_gate_status": "pass",
            "score_input_available_count": "0",
            "score_input_total_count": "0",
            "score_confidence": "0.9",
            "final_score": "50",
            "rank_ready_flag": "1",
            "rank_ready_reason": "ok",
            "model_status": "complete",
        }
    )
    return row


def subgroup_row(
    ticker: str,
    score: float,
    payload: dict[str, object],
    *,
    asof: str = "2026-08-21",
) -> dict[str, object]:
    recipe = payload["group_recipes"]["surface::rail"]
    return {
        "asof_date": asof,
        "ticker": ticker,
        "v8_cohort_id": "surface",
        "v8_group_id": "rail",
        "ranking_mode": "ranked",
        "specialized_pack_active_flag": "1",
        "component_weights_json": json.dumps(
            recipe["component_weights_active"],
            sort_keys=True,
        ),
        "v8_group_percentile_score": score,
        "group_cross_section_ready_flag": "1",
        "group_specialized_ready_flag": "1",
        "source_score_sha256": "b" * 64,
    }


def test_shadow_subgroup_scorer_stamps_lineage_and_keeps_all_gates_zero() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    rows = build_shadow_subgroup_rank_rows(
        source_rows=[source_row("AAA"), source_row("BBB")],
        subgroup_score_rows=[
            subgroup_row("AAA", 90.0, payload),
            subgroup_row("BBB", 10.0, payload),
        ],
        lock_payload=payload,
    )

    assert [row["ticker"] for row in rows] == ["AAA", "BBB"]
    assert {row["portfolio_candidate_gate"] for row in rows} == {"0"}
    assert {row["oos_score_valid_flag"] for row in rows} == {"0"}
    assert {row["transportation_production_state"] for row in rows} == {"shadow"}
    assert {row["transportation_group_recipe_key"] for row in rows} == {
        "surface::rail"
    }
    assert [row["transportation_group_rank"] for row in rows] == ["1", "2"]
    assert all(len(row["transportation_group_recipe_sha256"]) == 64 for row in rows)
    assert {
        row["transportation_membership_effective_from"] for row in rows
    } == {"2026-08-21"}


def test_shadow_subgroup_scorer_refuses_activation() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    with pytest.raises(ValueError, match="activation is fail-closed"):
        build_shadow_subgroup_rank_rows(
            source_rows=[source_row("AAA"), source_row("BBB")],
            subgroup_score_rows=[
                subgroup_row("AAA", 90.0, payload),
                subgroup_row("BBB", 10.0, payload),
            ],
            lock_payload=payload,
            activation_enabled=True,
        )


def test_shadow_subgroup_scorer_rejects_incomplete_current_ticker_census() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    with pytest.raises(ValueError, match="current subgroup ticker census"):
        build_shadow_subgroup_rank_rows(
            source_rows=[source_row("AAA")],
            subgroup_score_rows=[subgroup_row("AAA", 90.0, payload)],
            lock_payload=payload,
        )


def test_shadow_subgroup_scorer_rejects_wrong_applied_recipe() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    bad = subgroup_row("AAA", 90.0, payload)
    bad["component_weights_json"] = json.dumps(
        payload["group_recipes"]["surface::rail"][
            "component_weights_fallback"
        ]
    )
    with pytest.raises(ValueError, match="applied component weights"):
        build_shadow_subgroup_rank_rows(
            source_rows=[source_row("AAA"), source_row("BBB")],
            subgroup_score_rows=[bad, subgroup_row("BBB", 10.0, payload)],
            lock_payload=payload,
        )


def test_historical_membership_is_effective_dated_and_fails_outside_interval() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    spec = validate_subgroup_lock_payload(payload)
    inside = spec.membership_for("OLD", date(2020, 6, 30))
    outside = spec.membership_for("OLD", date(2021, 1, 1))
    current_before_policy = spec.membership_for("AAA", date(2026, 8, 20))
    current_on_policy = spec.membership_for("AAA", date(2026, 8, 21))
    assert inside is not None
    assert inside.membership_scope == "historical_calibration_only"
    assert outside is None
    assert current_before_policy is None
    assert current_on_policy is not None


def test_pre_effective_current_recipe_replay_is_explicit_and_snapshot_bounded() -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    source = [
        source_row("AAA", asof="2026-07-30"),
        source_row("BBB", asof="2026-07-30"),
    ]
    scores = [
        subgroup_row("AAA", 90.0, payload, asof="2026-07-30"),
        subgroup_row("BBB", 10.0, payload, asof="2026-07-30"),
    ]
    with pytest.raises(ValueError, match="no point-in-time/current"):
        build_shadow_subgroup_rank_rows(
            source_rows=source,
            subgroup_score_rows=scores,
            lock_payload=payload,
        )
    rows = build_shadow_subgroup_rank_rows(
        source_rows=source,
        subgroup_score_rows=scores,
        lock_payload=payload,
        allow_pre_effective_diagnostic_replay=True,
    )
    assert {
        row["transportation_membership_scope"] for row in rows
    } == {"pre_effective_policy_diagnostic_replay"}
    assert {
        row["transportation_membership_effective_from"] for row in rows
    } == {"2026-07-30"}
    assert {
        row["transportation_membership_effective_to"] for row in rows
    } == {"2026-07-30"}
    assert {row["portfolio_candidate_gate"] for row in rows} == {"0"}


def test_shadow_publisher_selects_locked_census_and_audits_excluded_rows() -> None:
    publisher = load_shadow_publisher()
    source = [
        source_row("AAA"),
        source_row("BBB"),
        source_row("OUT"),
    ]
    subgroup = [
        {"asof_date": "2026-08-21", "ticker": "AAA"},
        {"asof_date": "2026-08-21", "ticker": "BBB"},
    ]
    selected_source, selected_subgroup, audit = (
        publisher.select_policy_census_rows(
            source_rows=source,
            subgroup_rows=subgroup,
            asof="2026-08-21",
            expected_tickers={"AAA", "BBB"},
        )
    )
    assert [row["ticker"] for row in selected_source] == ["AAA", "BBB"]
    assert [row["ticker"] for row in selected_subgroup] == ["AAA", "BBB"]
    assert audit["source_asof_row_count"] == 3
    assert audit["selected_policy_ticker_count"] == 2
    assert audit["excluded_source_row_count"] == 1
    assert audit["excluded_source_tickers"] == ["OUT"]


def test_shadow_publisher_rejects_missing_or_duplicate_policy_tickers() -> None:
    publisher = load_shadow_publisher()
    with pytest.raises(ValueError, match="missing locked tickers"):
        publisher.select_policy_census_rows(
            source_rows=[source_row("AAA")],
            subgroup_rows=[
                {"asof_date": "2026-08-21", "ticker": "AAA"},
                {"asof_date": "2026-08-21", "ticker": "BBB"},
            ],
            asof="2026-08-21",
            expected_tickers={"AAA", "BBB"},
        )
    with pytest.raises(ValueError, match="duplicate tickers"):
        publisher.select_policy_census_rows(
            source_rows=[source_row("AAA"), source_row("AAA")],
            subgroup_rows=[
                {"asof_date": "2026-08-21", "ticker": "AAA"},
            ],
            asof="2026-08-21",
            expected_tickers={"AAA"},
        )


def test_shadow_publisher_uses_supplement_only_for_missing_policy_tickers() -> None:
    publisher = load_shadow_publisher()
    primary_aaa = source_row("AAA")
    supplement_aaa = source_row("AAA")
    supplement_aaa["company_name"] = "ignored duplicate source"
    supplement_bbb = source_row("BBB")
    selected_source, _selected_subgroup, audit = (
        publisher.select_policy_census_rows(
            source_rows=[primary_aaa, source_row("OUT")],
            source_supplement_rows=[supplement_aaa, supplement_bbb],
            subgroup_rows=[
                {"asof_date": "2026-08-21", "ticker": "AAA"},
                {"asof_date": "2026-08-21", "ticker": "BBB"},
            ],
            asof="2026-08-21",
            expected_tickers={"AAA", "BBB"},
        )
    )
    by_ticker = {row["ticker"]: row for row in selected_source}
    assert by_ticker["AAA"]["company_name"] == "AAA Corp"
    assert by_ticker["BBB"]["company_name"] == "BBB Corp"
    assert audit["source_supplement_fill_tickers"] == ["BBB"]
    assert audit["source_supplement_overlap_tickers"] == ["AAA"]
    assert audit["excluded_source_tickers"] == ["OUT"]


def test_dedicated_shadow_publish_never_creates_research_sidecar(
    tmp_path: Path,
) -> None:
    publisher = load_shadow_publisher()
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    rows = build_shadow_subgroup_rank_rows(
        source_rows=[source_row("AAA"), source_row("BBB")],
        subgroup_score_rows=[
            subgroup_row("AAA", 90.0, payload),
            subgroup_row("BBB", 10.0, payload),
        ],
        lock_payload=payload,
    )
    manifest = publisher.publish_subgroup_shadow_dashboard(
        output_dir=tmp_path,
        rows=rows,
        asof="2026-08-21",
    )
    assert manifest["row_count"] == 2
    assert manifest["stage11_survivorship_calibration_panel_row_count"] == 0
    assert (tmp_path / "transportation_final_rank_table.csv").is_file()
    assert not (
        tmp_path
        / "transportation_stage11_survivorship_calibration_panel.csv"
    ).exists()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        publisher.publish_subgroup_shadow_dashboard(
            output_dir=tmp_path,
            rows=rows,
            asof="2026-08-21",
        )


def test_shadow_publisher_requires_truth_labeled_hash_bound_research_lineage(
    tmp_path: Path,
) -> None:
    publisher = load_shadow_publisher()
    conflict_path = tmp_path / "conflict.json"
    score_csv = tmp_path / "scores.csv"
    score_manifest_path = tmp_path / "scores.json"
    calibration_path = tmp_path / "calibration.json"
    score_csv.write_text("asof_date,ticker\n2026-07-30,AAA\n", encoding="utf-8")
    conflict = {
        "policy_version": "transportation_accepted_fact_conflict_resolution_v3",
        "period_start_boundary_policy": (
            "complete_and_equal_for_every_deterministic_resolution_rule"
        ),
        "unresolved_fail_closed_count": 1707,
        "resolver_conflict_count_after": 1707,
        "production_activation_authorized": False,
    }
    conflict_path.write_text(json.dumps(conflict), encoding="utf-8")
    conflict_hash = publisher.file_sha256(conflict_path)
    score = {
        "conflict_resolution_bridge": {
            "status": "VERIFIED",
            "audit_sha256": conflict_hash,
        },
        "lineage": {"conflict_audit": {"sha256": conflict_hash}},
        "artifacts": {
            "score_history": {
                "path": str(score_csv.resolve()),
                "sha256": publisher.file_sha256(score_csv),
            }
        },
        "production_activation_authorized": False,
    }
    score_manifest_path.write_text(json.dumps(score), encoding="utf-8")
    calibration = {
        "contract_version": (
            "transportation_v8_subgroup_diagnostic_calibration_v3"
        ),
        "execution_acceptance": "PASS",
        "predictive_acceptance": "FAIL",
        "production_promotion_eligible": False,
        "production_activation_authorized": False,
        "lineage": {
            "conflict_audit": {"sha256": conflict_hash},
            "score_manifest": {
                "sha256": publisher.file_sha256(score_manifest_path)
            },
        },
    }
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")

    lineage = publisher.verify_research_lineage(
        conflict_audit_path=conflict_path,
        score_manifest_path=score_manifest_path,
        calibration_manifest_path=calibration_path,
        subgroup_score_path=score_csv,
    )
    assert lineage["execution_acceptance"] == "PASS"
    assert lineage["predictive_acceptance"] == "FAIL"
    assert lineage["production_promotion_eligible"] is False

    calibration["acceptance"] = "PASS"
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous top-level acceptance"):
        publisher.verify_research_lineage(
            conflict_audit_path=conflict_path,
            score_manifest_path=score_manifest_path,
            calibration_manifest_path=calibration_path,
            subgroup_score_path=score_csv,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.__setitem__(
                "production_activation_authorized", True
            ),
            "non-authorizing",
        ),
        (
            lambda payload: payload["group_recipes"]["surface::rail"].update(
                {"specialized_activation": "anything_goes"}
            ),
            "specialized_activation",
        ),
        (
            lambda payload: payload["group_recipes"]["surface::rail"][
                "component_weights_fallback"
            ].update({"specialized": 0.1, "market_trend": 0.9}),
            "fallback specialized weight",
        ),
        (
            lambda payload: payload["group_recipes"].pop("surface::rail"),
            "no group recipes",
        ),
    ],
)
def test_subgroup_lock_rejects_fail_open_or_incomplete_payloads(
    mutate,
    message: str,
) -> None:
    payload = build_subgroup_lock_payload(policy(), policy_sha256=POLICY_SHA256)
    mutated = copy.deepcopy(payload)
    mutate(mutated)
    with pytest.raises(ValueError, match=message):
        validate_subgroup_lock_payload(mutated)


def test_real_v8_policy_builds_complete_non_authorizing_lock_payload() -> None:
    from industrials.core.config import load_yaml

    root = Path(__file__).resolve().parents[2]
    source = root / "industrials" / "transportation" / "data" / "transportation_subgroup_score_policy_v8.yaml"
    payload = build_subgroup_lock_payload(
        load_yaml(source),
        policy_sha256=POLICY_SHA256,
    )
    spec = validate_subgroup_lock_payload(payload)
    assert len(spec.groups) == 6
    assert len(spec.memberships) == 44
    assert payload["production_activation_authorized"] is False
    assert payload["future_only_evidence_passed"] is False
