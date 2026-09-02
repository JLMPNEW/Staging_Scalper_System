from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from biotech_index.core.calibration_metrics import MetricSettings, summarize_returns
from biotech_index.core.calibration_provenance import observation_scoring_config_hash


def test_return_summary_includes_required_distribution_metrics() -> None:
    summary = summarize_returns(
        [0.20, 0.10, -0.10, -0.20],
        MetricSettings(min_profit_factor_wins=1, min_profit_factor_losses=1),
    )
    assert float(str(summary["ucb_return_pct"])) >= float(str(summary["lcb_return_pct"]))
    assert float(str(summary["sortino_like"])) == 0.0
    assert float(str(summary["omega_ratio"])) == 1.0
    assert float(str(summary["worst_decile_return_pct"])) == -20.0


def base_config() -> dict[str, object]:
    return {
        "biotech_scoring": {
            "production_baseline": {"selection_policy": "core_structural_veto"},
            "weights": {"catalyst": 0.3, "risk_penalty": 0.2},
            "investment_weight_profiles": {"clinical_stage": {"clinical_opportunity": 1.0}},
        },
        "biotech_taxonomy": {"version": "v5"},
        "calibration": {"walk_forward": {"optuna_trials": 500}},
    }


def test_observation_scoring_hash_ignores_search_only_settings_and_tracks_live_policy(tmp_path: Path) -> None:
    config = base_config()
    initial = observation_scoring_config_hash(config, base_dir=tmp_path)

    search_only = deepcopy(config)
    search_only["calibration"] = {"walk_forward": {"optuna_trials": 10}}  # type: ignore[index]
    assert observation_scoring_config_hash(search_only, base_dir=tmp_path) == initial

    live_policy = deepcopy(config)
    live_policy["biotech_scoring"]["production_baseline"]["selection_policy"] = "raw_legacy_score"  # type: ignore[index]
    assert observation_scoring_config_hash(live_policy, base_dir=tmp_path) != initial

def test_observation_scoring_hash_tracks_effective_cohort_migration_content(tmp_path: Path) -> None:
    cohort_path = tmp_path / "cohorts.csv"
    migration_path = tmp_path / "migration.csv"
    cohort_path.write_text("ticker,calibration_cohort\nAAA,commercial_profitable_quality_or_mature\n", encoding="utf-8")
    migration_path.write_text(
        "ticker,prior_cohort,new_cohort,effective_date\n"
        "AAA,late_clinical_pivotal_or_registrational,commercial_profitable_quality_or_mature,2026-08-31\n",
        encoding="utf-8",
    )
    config = base_config()
    config["biotech_scoring"]["calibration_cohorts"] = {  # type: ignore[index]
        "csv": cohort_path.name,
        "migration_csv": migration_path.name,
    }
    initial = observation_scoring_config_hash(config, base_dir=tmp_path)
    migration_path.write_text(
        "ticker,prior_cohort,new_cohort,effective_date\n"
        "AAA,platform_partnered_modality_pipeline,commercial_profitable_quality_or_mature,2026-08-31\n",
        encoding="utf-8",
    )
    assert observation_scoring_config_hash(config, base_dir=tmp_path) != initial
