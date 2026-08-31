from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from industrials.core.oos_research import evaluate_candidate
from industrials.transportation.financial_contract import (
    MetricDefinition,
    is_rankable_metric_value,
)
from industrials.transportation.scoring import metric_percentiles
from industrials.transportation.subgroup_production_lock import (
    canonical_sha256,
    validate_subgroup_lock_payload,
)
from industrials.transportation.subgroup_scoring import (
    ambiguous_fact_identity_counts,
    build_fact_history,
    build_v8_score_rows,
    derive_feature,
    resolver_selection_conflict_counts,
)
from industrials.transportation.surface_freight_score_engine import (
    percentile_scores as surface_percentiles,
)
from industrials.transportation.walk_forward_calibration import (
    percentile_scores as calibration_percentiles,
    purged_expanding_walk_forward_blocks,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "industrials" / "transportation" / "scripts"


def load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(
        "transportation_regression_" + name.replace(".", "_"),
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def accepted(
    *,
    value: float,
    period_end: str,
    filing_date: str,
    definition: str = "consolidated_ratio",
    source: str,
    accession: str = "",
) -> dict[str, object]:
    return {
        "ticker": "AAA",
        "metric_id": "operating_ratio",
        "value": value,
        "unit": "ratio",
        "period_end": period_end,
        "filing_date": filing_date,
        "accepted_at": filing_date,
        "definition_basis": definition,
        "comparability_class": "issuer_stable",
        "candidate_key": source,
        "accession_number": accession,
        "replay_status": "ACCEPTED",
    }


def identity_feature(history, asof: str):
    return derive_feature(
        ticker="AAA",
        asof=date.fromisoformat(asof),
        spec={"source_metric": "operating_ratio", "transform": "identity"},
        history=history,
        staleness_days={"operating_ratio": 800},
    )


def test_current_fact_prefers_latest_economic_period_over_later_old_filing() -> None:
    history = build_fact_history(
        [
            accepted(
                value=0.85,
                period_end="2025-12-31",
                filing_date="2026-02-15",
                source="current-period",
            ),
            accepted(
                value=0.95,
                period_end="2024-12-31",
                filing_date="2026-03-15",
                source="late-old-restatement",
            ),
        ]
    )
    value, sources = identity_feature(history, "2026-04-01")
    assert value == pytest.approx(0.85)
    assert sources == ("current-period",)


def test_conflicting_latest_scope_fails_closed_and_diagnostic_matches() -> None:
    history = build_fact_history(
        [
            accepted(
                value=0.585,
                period_end="2025-03-31",
                filing_date="2025-05-01",
                definition="consolidated",
                source="consolidated",
            ),
            accepted(
                value=0.602,
                period_end="2025-03-31",
                filing_date="2025-05-01",
                definition="rail-segment",
                source="segment",
            ),
        ]
    )
    assert identity_feature(history, "2025-06-01") == (None, ())
    assert ambiguous_fact_identity_counts(history) == {}
    assert resolver_selection_conflict_counts(history) == {"operating_ratio": 1}


def test_yoy_prior_cannot_use_restatement_disclosed_after_current_fact() -> None:
    history = build_fact_history(
        [
            accepted(
                value=0.90,
                period_end="2024-12-31",
                filing_date="2025-02-15",
                source="prior-as-reported",
            ),
            accepted(
                value=0.80,
                period_end="2024-12-31",
                filing_date="2026-03-15",
                source="later-restatement",
            ),
            accepted(
                value=0.85,
                period_end="2025-12-31",
                filing_date="2026-02-15",
                source="current",
            ),
        ]
    )
    value, sources = derive_feature(
        ticker="AAA",
        asof=date(2026, 4, 1),
        spec={
            "source_metric": "operating_ratio",
            "transform": "yoy_improvement",
        },
        history=history,
        staleness_days={"operating_ratio": 800},
    )
    assert value == pytest.approx((0.90 - 0.85) / 0.90)
    assert sources == ("current", "prior-as-reported")


def test_specialized_activation_is_point_in_time_not_global_latest_gate() -> None:
    components = (
        "market_trend",
        "quality",
        "growth",
        "valuation",
        "operating_efficiency",
        "capital_risk",
    )
    generic_recipes = {
        component: {"generic_metric": {"weight": 1.0, "direction": 1}}
        for component in components
    }
    active_weights = {
        "market_trend": 0.0,
        "quality": 0.0,
        "growth": 0.0,
        "valuation": 0.0,
        "operating_efficiency": 0.0,
        "capital_risk": 0.0,
        "positioning": 0.0,
        "specialized": 1.0,
    }
    fallback_weights = dict(active_weights)
    fallback_weights["specialized"] = 0.0
    fallback_weights["market_trend"] = 1.0
    policy = {
        "controls": {
            "neutral_missing_score": 50.0,
            "winsor_lower": 0.0,
            "winsor_upper": 1.0,
            "minimum_specialized_date_fraction": 0.5,
        },
        "generic_metric_recipes": generic_recipes,
        "cohorts": {
            "surface": {
                "groups": {
                    "ltl": {
                        "tickers": ["AAA", "BBB"],
                        "ranking_mode": "ranked",
                        "minimum_cross_section": 1,
                        "minimum_specialized_breadth": 2,
                        "specialized_activation": "coverage_gated_optional",
                        "component_weights_active": active_weights,
                        "component_weights_fallback": fallback_weights,
                        "specialized_pack": {
                            "pricing": {
                                "weight": 1.0,
                                "source_metric": "pricing_or_yield_growth",
                                "transform": "identity",
                                "direction": 1,
                            }
                        },
                    }
                }
            }
        },
        "historical_calibration_only": {},
    }

    def panel(asof: str, ticker: str) -> dict[str, object]:
        return {
            "asof_date": asof,
            "ticker": ticker,
            "horizon_sessions": "63",
            "metric_values_json": json.dumps({"generic_metric": 1.0}),
            "metric_status_json": json.dumps({"generic_metric": "REPORTED"}),
            "positioning_score": "50",
            "rank_ready_flag": "1",
            "calibration_eligible_flag": "1",
            "source_score_sha256": "a" * 64,
        }

    panel_rows = [
        panel("2025-01-31", "AAA"),
        panel("2025-01-31", "BBB"),
        panel("2025-02-28", "AAA"),
    ]
    accepted_rows = [
        {
            "ticker": ticker,
            "metric_id": "pricing_or_yield_growth",
            "value": value,
            "unit": "ratio",
            "period_end": "2025-01-31",
            "filing_date": "2025-01-31",
            "accepted_at": "2025-01-31",
            "definition_basis": "issuer_stable",
            "comparability_class": "issuer_stable",
            "candidate_key": f"{ticker}-pricing",
            "replay_status": "ACCEPTED",
        }
        for ticker, value in (("AAA", 0.01), ("BBB", 0.02))
    ]
    scores, coverage, manifest = build_v8_score_rows(
        panel_rows=panel_rows,
        accepted_rows=accepted_rows,
        policy=policy,
        staleness_days={"pricing_or_yield_growth": 0},
    )
    active_by_date = {
        (row["asof_date"], row["ticker"]): row["specialized_pack_active_flag"]
        for row in scores
    }
    assert active_by_date[("2025-01-31", "AAA")] == 1
    assert active_by_date[("2025-01-31", "BBB")] == 1
    assert active_by_date[("2025-02-28", "AAA")] == 0
    assert [row["applicable_ticker_count"] for row in coverage] == [2, 1]
    assert manifest["specialized_score_activation_policy"].startswith(
        "point_in_time"
    )


@pytest.mark.parametrize("ranker", [surface_percentiles, calibration_percentiles])
def test_percentile_ties_use_average_observation_ranks(ranker) -> None:
    scores = ranker({"A": 1.0, "B": 1.0, "C": 3.0})
    assert scores["A"] == pytest.approx(25.0)
    assert scores["B"] == pytest.approx(25.0)
    assert scores["C"] == pytest.approx(100.0)


def test_ev_operating_income_nonpositive_value_is_not_ranked_as_cheap() -> None:
    definition = MetricDefinition(
        metric_id="ev_operating_income",
        component="valuation",
        source="derived",
        source_field="",
        formula="enterprise_value / operating_income_ttm",
        candidate_metric="",
        direction=-1,
        cohorts=("cohort",),
        industries=(),
        required_for_rank=False,
        specialized=False,
        unit="multiple",
        minimum_history_days=0,
        winsor_lower=0.0,
        winsor_upper=1.0,
        birthdate="",
        production_status="active",
    )
    members = [
        {"ticker": ticker, "calibration_cohort_id": "cohort", "industry": "Railroads"}
        for ticker in ("A", "B", "C")
    ]
    metric_rows = {
        "A": {"ev_operating_income": {"availability_status": "DERIVED", "metric_value": -2}},
        "B": {"ev_operating_income": {"availability_status": "DERIVED", "metric_value": 2}},
        "C": {"ev_operating_income": {"availability_status": "DERIVED", "metric_value": 4}},
    }
    scores = metric_percentiles(members, [definition], metric_rows)
    assert not is_rankable_metric_value("ev_operating_income", -2)
    assert "ev_operating_income" not in scores["A"]
    assert scores["B"]["ev_operating_income"] == pytest.approx(100.0)
    assert scores["C"]["ev_operating_income"] == pytest.approx(0.0)


def test_walk_forward_purges_training_outcomes_crossing_test_start() -> None:
    rows = [
        {
            "asof_date": asof,
            "benchmark_exit_date": exit_date,
            "horizon_sessions": "63",
            "calibration_eligible_flag": "1",
            "outcome_available_flag": "1",
        }
        for asof, exit_date in (
            ("2020-01-01", "2020-04-01"),
            ("2020-02-01", "2020-02-20"),
            ("2020-03-01", "2020-06-01"),
            ("2020-04-01", "2020-07-01"),
        )
    ]
    blocks = purged_expanding_walk_forward_blocks(
        rows,
        horizon_sessions=63,
        block_count=1,
        minimum_initial_dates=2,
    )
    assert len(blocks) == 1
    assert blocks[0]["training_dates"] == ("2020-02-01",)
    assert blocks[0]["embargo_dates"] == ("2020-01-01",)


def outcome_row(
    ticker: str,
    *,
    score: float,
    security_return: float,
    benchmark_return: float = 0.02,
    security_exit: str = "2024-03-01",
    benchmark_exit: str = "2024-03-01",
    terminal_type: str = "",
    outcome_method: str = "scheduled_d1_open_to_open",
) -> dict[str, object]:
    return {
        "split": "diagnostic",
        "asof_date": "2024-01-31",
        "ticker": ticker,
        "horizon_sessions": "21",
        "calibration_eligible_flag": "1",
        "outcome_available_flag": "1",
        "v8_score": score,
        "entry_date": "2024-02-01",
        "exit_date": security_exit,
        "benchmark_entry_date": "2024-02-01",
        "benchmark_exit_date": benchmark_exit,
        "security_forward_return": security_return,
        "benchmark_forward_return": benchmark_return,
        "forward_excess_return": security_return - benchmark_return,
        "terminal_type": terminal_type,
        "outcome_method": outcome_method,
    }


def test_independent_schedule_retains_valid_early_terminal_outcome() -> None:
    rows = [
        outcome_row(
            "AAA",
            score=3.0,
            security_return=0.10,
            security_exit="2024-02-15",
            terminal_type="acquisition",
            outcome_method="terminal_membership_exit",
        ),
        outcome_row("BBB", score=2.0, security_return=0.03),
        outcome_row("CCC", score=1.0, security_return=0.01),
    ]
    rows[1]["entry_date"] = "2024-02-05"
    result = evaluate_candidate(
        rows,
        weights={"v8_score": 1.0},
        split="diagnostic",
        horizon_sessions=21,
        top_fraction=1 / 3,
        minimum_cross_section=3,
        transaction_cost_bps=20.0,
        require_complete_components=True,
        require_unique_benchmark_interval=True,
    )
    assert result["snapshot_count"] == 1
    assert result["invalid_execution_interval_cross_section_count"] == 0
    assert result["early_terminal_observation_count"] == 1
    assert result["late_security_entry_observation_count"] == 1
    assert result["period_rows"][0]["entry_date"] == "2024-02-01"
    assert result["period_rows"][0]["exit_date"] == "2024-03-01"
    assert result["period_rows"][0]["turnover"] == pytest.approx(1.0)
    assert result["mean_independent_top_excess_net"] == pytest.approx(0.078)
    assert result["terminal_proceeds_policy"] == (
        "terminal_proceeds_cash_carry_to_benchmark_exit_zero_return"
    )
    assert result["late_security_entry_policy"] == (
        "cash_carry_from_benchmark_entry_to_security_entry_zero_return"
    )


def test_nonunique_benchmark_interval_excludes_cross_section_fail_closed() -> None:
    rows = [
        outcome_row("AAA", score=3.0, security_return=0.10),
        outcome_row(
            "BBB",
            score=2.0,
            security_return=0.03,
            benchmark_exit="2024-03-04",
        ),
        outcome_row("CCC", score=1.0, security_return=0.01),
    ]
    result = evaluate_candidate(
        rows,
        weights={"v8_score": 1.0},
        split="diagnostic",
        horizon_sessions=21,
        minimum_cross_section=3,
        require_complete_components=True,
        require_unique_benchmark_interval=True,
    )
    assert result["snapshot_count"] == 0
    assert result["independent_snapshot_count"] == 0
    assert result["invalid_execution_interval_cross_section_count"] == 1
    assert result["invalid_execution_interval_cross_sections"][0]["reasons"] == [
        "benchmark_exit_date_missing_or_nonunique"
    ]


def test_independent_schedule_uses_benchmark_interval_not_signal_date() -> None:
    first = [
        outcome_row("AAA", score=3.0, security_return=0.05),
        outcome_row("BBB", score=2.0, security_return=0.03),
        outcome_row("CCC", score=1.0, security_return=0.01),
    ]
    second = [dict(row) for row in first]
    for row in second:
        row.update(
            asof_date="2024-02-09",
            entry_date="2024-02-12",
            exit_date="2024-03-15",
            benchmark_entry_date="2024-02-12",
            benchmark_exit_date="2024-03-15",
        )
    result = evaluate_candidate(
        first + second,
        weights={"v8_score": 1.0},
        split="diagnostic",
        horizon_sessions=21,
        minimum_cross_section=3,
        require_complete_components=True,
        require_unique_benchmark_interval=True,
    )
    assert result["snapshot_count"] == 2
    assert result["independent_snapshot_count"] == 1
    assert result["independent_intervals"] == [
        {
            "asof_date": "2024-01-31",
            "entry_date": "2024-02-01",
            "exit_date": "2024-03-01",
            "evaluation_available_flag": 1,
        }
    ]


def test_v8_gates_ignore_overlapping_statistics() -> None:
    module = load_script("42_run_transportation_v8_subgroup_calibration.py")
    metrics = {
        "outcome_coverage": 1.0,
        "snapshot_count": 20,
        "mean_ic": 0.50,
        "mean_top_excess_net": 0.10,
        "top_excess_hit_rate": 0.90,
        "mean_top_minus_cohort_net": 0.08,
        "mean_top_minus_bottom_gross": 0.10,
        "non_overlapping_snapshot_count": 6,
        "independent_outcome_coverage": 1.0,
        "mean_independent_ic": -0.10,
        "mean_independent_top_excess_net": -0.01,
        "independent_top_excess_hit_rate": 0.40,
        "mean_independent_top_minus_cohort_net": -0.02,
        "mean_independent_top_minus_bottom_gross": -0.03,
    }
    gates = {
        "minimum_ic": 0.0,
        "minimum_top_minus_group_net": 0.0,
        "minimum_top_minus_bottom_gross": 0.0,
        "minimum_hit_rate": 0.50,
        "minimum_non_overlapping_snapshots_per_block": 6,
    }
    result = module.gate_row(
        cohort_id="surface",
        group_id="rail",
        ranking_mode="ranked",
        horizon=63,
        block="diagnostic_block_1",
        metrics=metrics,
        gates=gates,
    )
    assert result["ranking_gate"] == "FAIL"
    assert result["investability_gate"] == "FAIL"
    assert result["group_gate"] == "FAIL"


def test_integrated_parcel_predictive_gate_is_not_applicable() -> None:
    module = load_script("42_run_transportation_v8_subgroup_calibration.py")
    result = module.non_ranked_group_summary("eligibility_equal_weight")
    assert result["predictive_gate_applicability"] == "NOT_APPLICABLE"
    assert result["all_fixed_blocks_pass"] is None


def test_calibration_truth_labels_do_not_conflate_execution_and_prediction() -> None:
    module = load_script("42_run_transportation_v8_subgroup_calibration.py")
    labels = module.calibration_truth_labels(
        {
            "surface::rail": {"all_fixed_blocks_pass": False},
            "surface::parcel": {"all_fixed_blocks_pass": None},
        }
    )
    assert labels == {
        "execution_acceptance": "PASS",
        "predictive_acceptance": "FAIL",
        "predictive_failure_groups": ["surface::rail"],
        "production_promotion_eligible": False,
    }
    assert "acceptance" not in labels


def test_v8_fixed_blocks_require_exit_containment_and_keep_turnover() -> None:
    module = load_script("42_run_transportation_v8_subgroup_calibration.py")
    blocks = [
        {"block_id": "one", "start_date": "2021-01-01", "end_date": "2021-12-31"},
        {"block_id": "two", "start_date": "2022-01-01", "end_date": "2022-12-31"},
    ]
    assert module.strict_block_id("2021-12-01", "2022-02-01", blocks) is None
    source_rows = [
        outcome_row("AAA", score=3.0, security_return=0.05),
        outcome_row("BBB", score=2.0, security_return=0.03),
        outcome_row("CCC", score=1.0, security_return=0.01),
    ]
    for row in source_rows:
        row.update(
            asof_date="2022-01-05",
            horizon_sessions="63",
            entry_date="2022-01-06",
            exit_date="2022-03-31",
            benchmark_entry_date="2022-01-06",
            benchmark_exit_date="2022-03-31",
        )
    metrics = module.summarize_fixed_block(
        source_rows=source_rows,
        horizon=63,
        block="two",
        blocks=blocks,
        top_fraction=1 / 3,
        minimum_cross_section=3,
        transaction_cost_bps=20.0,
    )
    assert metrics["snapshot_count"] == 1
    assert metrics["average_turnover"] == pytest.approx(1.0)
    assert metrics["average_independent_turnover"] == pytest.approx(1.0)


def subgroup_payload() -> dict[str, object]:
    active = {component: 0.0 for component in (
        "market_trend", "quality", "growth", "valuation",
        "operating_efficiency", "capital_risk", "positioning", "specialized",
    )}
    active["specialized"] = 1.0
    fallback = dict(active)
    fallback["specialized"] = 0.0
    fallback["market_trend"] = 1.0
    group_recipes = {
        "surface::rail": {
            "cohort_id": "surface",
            "group_id": "rail",
            "ranking_mode": "ranked",
            "tickers": ["AAA", "BBB"],
            "aggregate_group_weight": 1.0,
            "component_weights_active": active,
            "component_weights_fallback": fallback,
            "specialized_activation": "required_for_calibration",
            "minimum_cross_section": 2,
            "specialized_pack": {
                "operating_ratio_yoy_improvement": {
                    "weight": 1.0,
                    "source_metric": "operating_ratio",
                    "transform": "yoy_improvement",
                    "direction": 1,
                }
            },
        }
    }
    return {
        "scoring_mode": "subgroup_v8",
        "group_recipe_version": "transportation_subgroup_v8_lock_v1",
        "subgroup_policy_sha256": "a" * 64,
        "policy_effective_from": "2026-08-21",
        "group_recipes": group_recipes,
        "expected_group_count": 1,
        "expected_current_ticker_count": 2,
        "expected_group_keys_sha256": canonical_sha256(["surface::rail"]),
        "group_recipe_set_sha256": canonical_sha256(group_recipes),
        "production_activation_authorized": False,
        "future_only_evidence_passed": False,
    }


def test_subgroup_lock_recipe_validates_but_activation_remains_fail_closed() -> None:
    payload = subgroup_payload()
    spec = validate_subgroup_lock_payload(payload)
    assert set(spec.groups) == {"surface::rail"}
    activation = load_script("31_activate_transportation_oos_production.py")
    with pytest.raises(ValueError, match="activation is fail-closed"):
        activation.validate_activation_scoring_mode(
            {"scoring_mode": "subgroup_v8"},
            payload,
        )
