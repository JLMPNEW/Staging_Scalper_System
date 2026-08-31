from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_runner() -> ModuleType:
    path = ROOT / "biotech_index" / "scripts" / "60_run_biotech_walk_forward_calibration.py"
    spec = importlib.util.spec_from_file_location("test_walk_forward_runner_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Spec:
    candidate_name: str


@dataclass(frozen=True)
class Policy:
    policy_name: str


class FakeCalibrationModule:
    CURRENT_CONFIG_CANDIDATE_NAME = "incumbent"

    @staticmethod
    def stable_candidate_id(spec: Spec, policy: Policy) -> str:
        return f"{spec.candidate_name}:{policy.policy_name}"

    @staticmethod
    def generate_weight_specs(_config: object, *, candidate_limit: int) -> list[Spec]:
        full = [Spec("incumbent"), Spec("challenger")]
        return full if candidate_limit == 0 else full[:candidate_limit]

    @staticmethod
    def objective_return_key(horizon: int, _params: object) -> str:
        return f"fwd_{horizon}d_net_benchmark_alpha_return"

    @staticmethod
    def selected_rows_by_date(
        rows: list[dict[str, object]],
        _spec: Spec,
        _policy: Policy,
        *,
        horizon: int,
        top_n: int,
        params: object,
    ) -> list[dict[str, object]]:
        del horizon, top_n, params
        selected: list[dict[str, object]] = []
        for row in rows:
            payload = dict(row)
            payload["candidate_selection_score"] = payload["score"]
            selected.append(payload)
        return selected


def test_candidate_limit_cannot_remove_production_incumbent() -> None:
    runner = load_runner()
    module = FakeCalibrationModule()
    policies = [Policy("core_structural_veto")]
    limited_specs = [Spec("challenger")]
    config = {
        "biotech_scoring": {
            "production_baseline": {
                "candidate_name": "incumbent",
                "selection_policy": "core_structural_veto",
            }
        }
    }
    specs, pairs, incumbent = runner.ensure_incumbent_in_grid(
        module,
        config,
        limited_specs,
        policies,
        candidate_limit=1,
    )
    assert {spec.candidate_name for spec in specs} == {"challenger", "incumbent"}
    assert incumbent[0] == "incumbent:core_structural_veto"
    assert incumbent[0] in pairs


def fold_plan_settings(runner: ModuleType, *, primary_horizon: int = 120) -> SimpleNamespace:
    return SimpleNamespace(
        primary_horizon=primary_horizon,
        windows={
            120: runner.WalkForwardWindow(
                horizon_bars=120,
                validation_months=12,
                test_months=12,
                step_months=12,
                embargo_days=185,
                min_training_years=3,
                min_train_dates=8,
                min_validation_dates=4,
                min_test_dates=4,
            )
        },
        promotion_rules=runner.PromotionRules(min_outer_folds=2),
    )


def fold_plan_rows(*, years: int) -> list[dict[str, object]]:
    return [
        {
            "asof_date": value,
            "fwd_120d_target_date": value,
            "fwd_120d_net_benchmark_alpha_return": 0.01,
        }
        for value in (
            (date(2019, 1, 1) + timedelta(days=offset)).isoformat()
            for offset in range(365 * years)
        )
    ]


def test_fold_plan_fails_before_scoring_when_primary_horizon_has_no_fold() -> None:
    runner = load_runner()
    with pytest.raises(ValueError, match="No complete walk-forward folds.*primary"):
        runner.build_fold_plan(
            fold_plan_rows(years=3),
            module=FakeCalibrationModule(),
            params=object(),
            settings=fold_plan_settings(runner),
        )


def test_fold_plan_supports_multiple_primary_outer_folds() -> None:
    runner = load_runner()
    plan = runner.build_fold_plan(
        fold_plan_rows(years=8),
        module=FakeCalibrationModule(),
        params=object(),
        settings=fold_plan_settings(runner),
    )
    assert len(plan[120]) >= 2


def test_incumbent_returns_fills_no_selection_dates_with_xbi_residual() -> None:
    runner = load_runner()

    class SparseCalibrationModule(FakeCalibrationModule):
        @staticmethod
        def selected_rows_by_date(
            rows: list[dict[str, object]],
            spec: Spec,
            policy: Policy,
            *,
            horizon: int,
            top_n: int,
            params: object,
        ) -> list[dict[str, object]]:
            return FakeCalibrationModule.selected_rows_by_date(
                [row for row in rows if row["ticker"] == "AAA"],
                spec,
                policy,
                horizon=horizon,
                top_n=top_n,
                params=params,
            )

    rows = [
        {
            "asof_date": "2024-01-05",
            "ticker": "AAA",
            "score": 60.0,
            "fwd_120d_net_benchmark_alpha_return": 0.10,
        },
        {
            "asof_date": "2024-01-12",
            "ticker": "BBB",
            "score": 55.0,
            "fwd_120d_net_benchmark_alpha_return": -0.20,
        },
    ]
    returns, records = runner.incumbent_returns(
        SparseCalibrationModule(),
        rows,
        Spec("incumbent"),
        Policy("core_structural_veto"),
        horizon=120,
        top_n=10,
        params=object(),
    )

    assert [record.ticker for record in records] == ["AAA"]
    assert returns == {"2024-01-05": pytest.approx(0.10), "2024-01-12": 0.0}


def test_cohort_comparison_aligns_dates_where_only_one_policy_is_active() -> None:
    runner = load_runner()
    rows = runner.cohort_comparisons(
        [runner.ReliabilityRecord("2024-01-05", "AAA", 60.0, 0.10, "platform")],
        [runner.ReliabilityRecord("2024-01-12", "BBB", 55.0, 0.20, "platform")],
        runner.MetricSettings(
            bootstrap_iterations=0,
            min_profit_factor_wins=1,
            min_profit_factor_losses=1,
        ),
        fold_id="h120_f01",
        horizon=120,
        active_weight=0.5,
    )

    assert len(rows) == 1
    assert rows[0]["paired_date_count"] == 2
    assert rows[0]["candidate_mean_return_pct"] == pytest.approx(2.5)
    assert rows[0]["incumbent_mean_return_pct"] == pytest.approx(10.0)


def test_deployable_fold_contract_excludes_research_payloads() -> None:
    runner = load_runner()
    payload = runner.deployable_fold_contract(
        {
            "candidate_id": "candidate_1",
            "candidate_pool_top_n": 20,
            "candidate_spec": {"candidate_name": "candidate"},
            "selection_policy": {"policy_name": "core_structural_veto"},
            "threshold": {"max_names": 10},
            "outer_test_comparison_row": {"fold_id": "h120_f01"},
            "signature": {"framework_version": "test"},
            "grid_rows": [{"candidate_id": "diagnostic_only"}],
            "candidate_records": [{"ticker": "AAA"}],
            "incumbent_records": [{"ticker": "BBB"}],
            "secondary_evaluations": [{"horizon_days": 60}],
        }
    )

    assert payload["candidate_id"] == "candidate_1"
    assert payload["threshold"] == {"max_names": 10}
    assert "grid_rows" not in payload
    assert "candidate_records" not in payload
    assert "incumbent_records" not in payload
    assert "secondary_evaluations" not in payload


def test_fold_grid_cache_uses_verified_sidecar(tmp_path: Path) -> None:
    runner = load_runner()
    rows = [
        {"fold_id": "h120_f01", "candidate_id": "candidate_1", "metric": 1.25},
        {"fold_id": "h120_f01", "candidate_id": "candidate_2", "metric": -0.5},
    ]

    metadata = runner.persist_fold_grid_rows(tmp_path, rows)
    assert "grid_rows" not in metadata
    assert metadata["grid_rows_file"] == "candidate_metrics.csv"
    assert metadata["grid_row_count"] == 2
    assert runner.load_cached_fold_grid_rows(metadata, tmp_path) == [
        {"fold_id": "h120_f01", "candidate_id": "candidate_1", "metric": "1.25"},
        {"fold_id": "h120_f01", "candidate_id": "candidate_2", "metric": "-0.5"},
    ]


def test_fold_grid_cache_rejects_modified_sidecar(tmp_path: Path) -> None:
    runner = load_runner()
    metadata = runner.persist_fold_grid_rows(tmp_path, [{"candidate_id": "candidate_1"}])
    (tmp_path / "candidate_metrics.csv").write_text("candidate_id\nchanged\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        runner.load_cached_fold_grid_rows(metadata, tmp_path)


def test_fold_grid_cache_reads_legacy_inline_rows(tmp_path: Path) -> None:
    runner = load_runner()
    payload = {"grid_rows": [{"candidate_id": "legacy"}]}

    assert runner.load_cached_fold_grid_rows(payload, tmp_path) == [{"candidate_id": "legacy"}]


def test_secondary_horizon_replays_the_frozen_primary_contract() -> None:
    runner = load_runner()
    rows = [
        {
            "asof_date": f"2024-01-{day:02d}",
            "ticker": f"T{day:02d}",
            "score": 100.0,
            "biotech_primary_cohort": "late_clinical_pivotal_or_registrational",
            "fwd_60d_net_benchmark_alpha_return": 0.01 if day % 2 else -0.005,
        }
        for day in range(1, 21)
    ]
    threshold = runner.ReliabilityThreshold(
        min_score_pct_of_top=90.0,
        max_names=8,
        reliability_class="medium",
        active_weight=0.55,
        validation_objective=1.0,
        validation_metrics={},
    )
    settings = SimpleNamespace(
        metric_settings=runner.MetricSettings(
            min_profit_factor_wins=1,
            min_profit_factor_losses=1,
            bootstrap_iterations=0,
        )
    )
    evaluation = runner.build_secondary_horizon_evaluation(
        FakeCalibrationModule(),
        rows,
        candidate_spec=Spec("frozen_primary"),
        candidate_policy=Policy("core_structural_veto"),
        candidate_id="frozen-id",
        candidate_name="frozen_primary",
        selection_policy_name="core_structural_veto",
        threshold=threshold,
        frozen_top_n=10,
        candidate_pool_top_n=20,
        incumbent_spec=Spec("incumbent"),
        incumbent_policy=Policy("core_structural_veto"),
        incumbent_top_n=10,
        horizon=60,
        params=object(),
        settings=settings,
        fold_id="h120_f01",
    )
    comparison = evaluation["outer_test_comparison_row"]
    assert isinstance(comparison, dict)
    assert comparison["candidate_id"] == "frozen-id"
    assert comparison["horizon_days"] == 60
    assert comparison["candidate_return_contract"] == "frozen_primary_policy_secondary_horizon"

    comparison_rows: list[dict[str, object]] = []
    candidate_returns: defaultdict[int, list[dict[str, float]]] = defaultdict(list)
    incumbent_returns: defaultdict[int, list[dict[str, float]]] = defaultdict(list)
    candidate_cohorts: defaultdict[int, defaultdict[str, list[dict[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    incumbent_cohorts: defaultdict[int, defaultdict[str, list[dict[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    candidate_regimes: defaultdict[int, defaultdict[str, list[dict[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    incumbent_regimes: defaultdict[int, defaultdict[str, list[dict[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    runner.ingest_frozen_evaluation(
        evaluation,
        regime_lookup={(row["asof_date"], row["ticker"]): "test" for row in rows},
        fold_comparison_rows=comparison_rows,
        cohort_rows=[],
        selected_rows_output=[],
        sleeve_rows_output=[],
        fold_candidate_returns=candidate_returns,
        fold_incumbent_returns=incumbent_returns,
        fold_candidate_cohort_returns=candidate_cohorts,
        fold_incumbent_cohort_returns=incumbent_cohorts,
        fold_candidate_regime_returns=candidate_regimes,
        fold_incumbent_regime_returns=incumbent_regimes,
    )
    assert comparison_rows == [comparison]
    assert len(candidate_returns[60]) == 1
    assert len(incumbent_returns[60]) == 1


def validation_metrics(*, delta_lcb: float) -> dict[str, object]:
    return {
        "paired_date_count": 20,
        "paired_delta_bootstrap_lcb_pct": delta_lcb,
        "candidate_mean_return_pct": 10.0,
        "incumbent_mean_return_pct": 5.0,
        "candidate_lcb_return_pct": 2.0,
        "incumbent_lcb_return_pct": 1.0,
        "candidate_hit_rate_pct": 60.0,
        "incumbent_hit_rate_pct": 50.0,
        "candidate_profit_factor": 1.5,
        "incumbent_profit_factor": 1.2,
        "delta_profit_factor": 1.2,
        "candidate_winsorized_profit_factor": 1.3,
        "candidate_profit_factor_ex_largest_winner": 1.2,
        "candidate_profit_factor_ex_top3_winners": 1.1,
        "candidate_loss20_rate_pct": 5.0,
        "incumbent_loss20_rate_pct": 5.0,
        "candidate_loss40_rate_pct": 1.0,
        "incumbent_loss40_rate_pct": 1.0,
        "candidate_cvar_return_pct": -10.0,
        "incumbent_cvar_return_pct": -10.0,
        "candidate_max_drawdown_pct": -15.0,
        "incumbent_max_drawdown_pct": -15.0,
        "candidate_top3_gain_contribution_pct": 40.0,
        "active_date_count": 20,
        "evaluation_date_count": 20,
    }


def runner_settings(runner: ModuleType) -> SimpleNamespace:
    return SimpleNamespace(
        candidate_pool_top_n=20,
        score_pct_candidates=(80.0,),
        max_name_candidates=(8,),
        metric_settings=runner.MetricSettings(
            min_profit_factor_wins=1,
            min_profit_factor_losses=1,
            bootstrap_iterations=0,
        ),
        active_weight_by_class={"high": 0.9, "medium": 0.55, "low": 0.2},
        promotion_rules=runner.PromotionRules(min_paired_dates=1),
        optuna_enabled=False,
    )


def test_failed_validation_candidate_cannot_become_outer_test_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_runner()
    record = runner.ReliabilityRecord("2026-01-02", "TEST", 50.0, 0.1)
    rejected_metrics = validation_metrics(delta_lcb=-1.0)
    rejected_metrics["candidate_loss40_rate_pct"] = 10.0
    rejected = runner.ReliabilityThreshold(
        min_score_pct_of_top=80.0,
        max_names=8,
        reliability_class="high",
        active_weight=0.9,
        validation_objective=999.0,
        validation_metrics=rejected_metrics,
    )
    monkeypatch.setattr(runner, "candidate_records", lambda *args, **kwargs: [record])
    monkeypatch.setattr(runner, "select_reliability_threshold", lambda *args, **kwargs: rejected)
    monkeypatch.setattr(runner, "build_reliability_curve", lambda *args, **kwargs: [])

    winner, _thresholds, _curves = runner.evaluate_validation_shortlist(
        object(),
        [],
        [("loser", Spec("loser"), Policy("core_structural_veto"), 10)],
        {"2026-01-02": 0.0},
        horizon=120,
        params=object(),
        settings=runner_settings(runner),
        min_dates=1,
        fold_id="h120_f01",
        trial_audit_rows=[],
    )
    assert winner is None


def test_validation_gate_balances_metrics_and_requires_robust_profit_factor_support() -> None:
    runner = load_runner()
    metrics = validation_metrics(delta_lcb=-1.0)
    assert runner.validation_candidate_survives(metrics, runner.PromotionRules(min_paired_dates=1))
    metrics["candidate_profit_factor_ex_top3_winners"] = ""
    assert runner.validation_candidate_survives(metrics, runner.PromotionRules(min_paired_dates=1))
    metrics["candidate_profit_factor_ex_largest_winner"] = ""
    assert not runner.validation_candidate_survives(metrics, runner.PromotionRules(min_paired_dates=1))


def test_optuna_trials_receive_selection_coverage_metrics() -> None:
    runner = load_runner()
    dates = [f"2026-01-{day:02d}" for day in range(1, 13)]
    candidate_returns = [0.08] * 8 + [-0.01] * 4
    incumbent_returns = [0.09] * 4 + [0.02] * 4 + [-0.04] * 4
    records = tuple(
        runner.ReliabilityRecord(asof_date, f"T{index:02d}", 100.0, return_value)
        for index, (asof_date, return_value) in enumerate(zip(dates, candidate_returns), start=1)
    )
    settings = SimpleNamespace(
        metric_settings=runner.MetricSettings(
            min_profit_factor_wins=1,
            min_profit_factor_losses=1,
            bootstrap_iterations=20,
        ),
        active_weight_by_class={"high": 1.0, "medium": 1.0, "low": 1.0},
        score_pct_candidates=(80.0,),
        max_name_candidates=(8,),
        optuna_seed=7331,
        optuna_trials=1,
        promotion_rules=runner.PromotionRules(min_paired_dates=8),
    )
    trials: list[dict[str, object]] = []
    winner = runner.optimize_with_optuna(
        {"candidate": (Spec("candidate"), Policy("core_structural_veto"), 10, records)},
        dict(zip(dates, incumbent_returns)),
        settings,
        min_dates=8,
        fold_id="h120_f01",
        horizon=120,
        trial_audit_rows=trials,
    )
    assert winner is not None
    assert winner.threshold.validation_metrics["active_date_count"] == 12
    assert winner.threshold.validation_metrics["evaluation_date_count"] == 12
