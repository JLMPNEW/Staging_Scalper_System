from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from technology.core.calibration_governance import (
    MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION,
    canonical_sha256,
    final_promotion_decision,
    incumbent_relative_cohort_cap,
    new_run_id,
    seal_calibration_run,
    sha256_file,
    stage8_gate_decision,
    validate_calibration_run_manifest,
    validate_promotion_receipt,
)
from technology.core.calibrated_scoring import scoring_settings_for_asof
from technology.core.oos_provenance import build_oos_provenance
from technology.core.optuna_artifact_governance import harden_stage8, validate_stage8
from technology.core.promotion_governance import resolve_production_binding
from technology.software_infrastructure.calibrated_scoring import SETTINGS as SOFTWARE_SETTINGS


def passing_stage8_metrics() -> dict[str, float]:
    return {
        "objective": 0.03,
        "mean_ic_21": 0.04,
        "mean_ic_63": 0.04,
        "newey_west_t_stat_21": 2.5,
        "newey_west_t_stat_63": 2.5,
        "hit_rate_21": 0.60,
        "mean_spread_net_21": 0.01,
        "mean_spread_net_63": 0.01,
        "avg_top_turnover": 0.30,
        "avg_top_cohort_share": 0.30,
    }


def test_stage8_override_is_never_preliminarily_promotable() -> None:
    decision = stage8_gate_decision(
        candidate=passing_stage8_metrics(),
        baseline={"objective": 0.0},
        primary_horizon=21,
        secondary_horizon=63,
        min_objective_improvement=0.002,
        min_ic_primary=0.005,
        min_ic_secondary=0.005,
        min_newey_west_t_primary=2.0,
        min_newey_west_t_secondary=2.0,
        min_hit_rate=0.50,
        min_spread_primary=0.0,
        min_spread_secondary=0.0,
        max_turnover=0.60,
        max_cohort_share=0.55,
        fold_win_fraction=0.80,
        min_fold_win_fraction=0.50,
        post_lock_data_included=True,
    )
    assert not decision.passed
    assert "post_lock_research_override" in decision.reasons


def test_stage8_requires_newey_west_significance() -> None:
    metrics = passing_stage8_metrics()
    metrics["newey_west_t_stat_63"] = 1.99
    decision = stage8_gate_decision(
        candidate=metrics,
        baseline={"objective": 0.0},
        primary_horizon=21,
        secondary_horizon=63,
        min_objective_improvement=0.002,
        min_ic_primary=0.005,
        min_ic_secondary=0.005,
        min_newey_west_t_primary=2.0,
        min_newey_west_t_secondary=2.0,
        min_hit_rate=0.50,
        min_spread_primary=0.0,
        min_spread_secondary=0.0,
        max_turnover=0.60,
        max_cohort_share=0.55,
        fold_win_fraction=0.80,
        min_fold_win_fraction=0.50,
        post_lock_data_included=False,
    )
    assert not decision.passed
    assert decision.reasons == ("newey_west_t_stat_63_below_minimum",)


def test_final_promotion_requires_matching_walk_forward_evidence() -> None:
    stage8 = {
        "stage8_gate_pass": 1,
        "post_lock_data_included": False,
        "config_sha256": "config-a",
        "signal_panel_sha256": "panel-a",
    }
    walk_forward = {
        "config_sha256": "config-a",
        "signal_panel_sha256": "panel-a",
        "post_lock_data_included": False,
        "refit_win_rate": 0.70,
        "promotion_gate_pass_rate": 0.70,
        "constraint_pass_rate": 1.0,
        "improvement_paired_t": 2.5,
        "mean_objective_improvement": 0.01,
    }
    decision = final_promotion_decision(
        stage8,
        walk_forward,
        min_paired_t=2.0,
        min_gate_pass_rate=0.50,
        min_win_rate=0.50,
        min_constraint_pass_rate=0.50,
    )
    assert decision.passed
    mismatched = dict(walk_forward, signal_panel_sha256="panel-b")
    decision = final_promotion_decision(
        stage8,
        mismatched,
        min_paired_t=2.0,
        min_gate_pass_rate=0.50,
        min_win_rate=0.50,
        min_constraint_pass_rate=0.50,
    )
    assert not decision.passed
    assert "stage8_walk_forward_panel_hash_mismatch" in decision.reasons


def test_sealed_run_detects_stale_and_tampered_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    config = tmp_path / "config.yaml"
    panel = tmp_path / "panel.csv"
    artifact = output / "result.csv"
    config.write_text("version: 1\n", encoding="utf-8")
    panel.write_text("asof_date,ticker\n2024-01-02,TEST\n", encoding="utf-8")
    artifact.write_text("metric,value\nic,0.1\n", encoding="utf-8")
    run_id = new_run_id(
        "software_infrastructure",
        "stage8",
        config_sha256=sha256_file(config),
        panel_sha256=sha256_file(panel),
    )
    seal_calibration_run(
        output_dir=output,
        manifest_filename="stage8_run_manifest.json",
        run_id=run_id,
        model_family="software_infrastructure",
        stage="stage8",
        config_path=config,
        panel_path=panel,
        artifact_names=["result.csv"],
    )
    manifest = output / "stage8_run_manifest.json"
    assert validate_calibration_run_manifest(
        manifest,
        expected_model_family="software_infrastructure",
        expected_stage="stage8",
        current_config_path=config,
        current_panel_path=panel,
    ) == []
    artifact.write_text("metric,value\nic,9.9\n", encoding="utf-8")
    errors = validate_calibration_run_manifest(
        manifest,
        expected_model_family="software_infrastructure",
        expected_stage="stage8",
        current_config_path=config,
        current_panel_path=panel,
    )
    assert "Compatibility artifact hash mismatch: result.csv" in errors
    config.write_text("version: 2\n", encoding="utf-8")
    errors = validate_calibration_run_manifest(
        manifest,
        expected_model_family="software_infrastructure",
        expected_stage="stage8",
        current_config_path=config,
        current_panel_path=panel,
    )
    assert any("config_sha256" in error for error in errors)


def test_incumbent_relative_cohort_cap_is_bounded_and_adaptive() -> None:
    assert incumbent_relative_cohort_cap(0.55, 0.40, 0.02) == 0.55
    assert incumbent_relative_cohort_cap(0.70, 0.79, 0.02) == 0.81
    assert incumbent_relative_cohort_cap(0.70, 0.99, 0.05) == 1.0


def test_run_ids_fit_deep_windows_output_paths() -> None:
    run_id = new_run_id(
        "technology_hardware",
        "walk_forward",
        config_sha256="c" * 64,
        panel_sha256="p" * 64,
    )
    assert run_id.startswith("hw_wf_")
    assert len(run_id) <= 64


def test_oos_model_version_cannot_reuse_invalid_lock_chronology() -> None:
    config = {
        "oos_calibration_standards": {
            "allow_replay_oos_within_days": 5,
            "families": {
                "semiconductors": {
                    "calibration_train_start_date": "2024-01-01",
                    "calibration_train_end_date": "2026-06-14",
                    "calibration_lock_date": "2026-06-15",
                    "calibration_production_start_date": "2026-06-15",
                    "production_model_version": "semiconductor_v2",
                    "production_model_effective_date": "2026-06-16",
                    "calibration_validation_method": "test",
                }
            },
        }
    }
    provenance = build_oos_provenance(
        config,
        model_family="semiconductors",
        asof="2026-06-15",
        historical_mode=False,
    )
    assert provenance.row_fields["scoring_weights_frozen_flag"] == 0
    assert "invalid_model_lock_date_chronology" in provenance.row_fields["oos_invalid_reason"]


def test_legacy_stage8_binding_is_explicit_not_falsely_sealed(tmp_path: Path) -> None:
    config_path = tmp_path / "technology.yaml"
    config_path.write_text("placeholder: true\n", encoding="utf-8")
    config = {
        "oos_calibration_standards": {
            "families": {
                "software_infrastructure": {
                    "production_model_version": "software_v1",
                    "production_model_effective_date": "2026-06-15",
                }
            }
        },
        "software_infrastructure_governance_reports": {
            "production_model_status": "stage8_active",
            "legacy_pre_receipt_model_version": "software_v1",
            "active_promotion_receipt_path": "",
        },
    }
    binding = resolve_production_binding(
        config,
        config_path=config_path,
        family="software_infrastructure",
        governance_config_key="software_infrastructure_governance_reports",
    )
    assert binding.valid
    assert binding.status == "legacy_pre_receipt_grandfathered"
    assert binding.reasons == ("immutable_candidate_artifacts_not_available_for_legacy_promotion",)


def test_scoring_schedule_preserves_v1_before_v2_effective_date() -> None:
    config = {
        "software_infrastructure_calibrated_scoring": {
            "model_schedule": [
                {
                    "start_date": "2010-01-01",
                    "end_date": "2026-08-30",
                    "weights_config_key": "software_v1",
                    "model_version": "v1",
                },
                {
                    "start_date": "2026-08-31",
                    "weights_config_key": "software_v2",
                    "model_version": "v2",
                },
            ]
        },
        "software_v1": {"model_version": "v1"},
        "software_v2": {"model_version": "v2"},
    }
    old = scoring_settings_for_asof(config, SOFTWARE_SETTINGS, "2026-08-30")
    new = scoring_settings_for_asof(config, SOFTWARE_SETTINGS, "2026-08-31")
    assert old.config_key == "software_v1"
    assert new.config_key == "software_v2"


def test_oos_schedule_reports_the_model_active_on_each_date() -> None:
    common = {
        "calibration_train_start_date": "2011-01-01",
        "calibration_train_end_date": "2026-06-14",
        "calibration_validation_method": "test",
    }
    config = {
        "oos_calibration_standards": {
            "families": {
                "software_infrastructure": {
                    **common,
                    "model_schedule": [
                        {
                            **common,
                            "calibration_lock_date": "2026-06-15",
                            "calibration_production_start_date": "2026-06-15",
                            "production_model_effective_date": "2026-06-15",
                            "production_model_version": "v1",
                            "end_date": "2026-08-30",
                        },
                        {
                            **common,
                            "calibration_lock_date": "2026-08-29",
                            "calibration_production_start_date": "2026-08-31",
                            "production_model_effective_date": "2026-08-31",
                            "production_model_version": "v2",
                        },
                    ],
                }
            }
        }
    }
    old = build_oos_provenance(config, model_family="software_infrastructure", asof="2026-08-30", historical_mode=False)
    new = build_oos_provenance(config, model_family="software_infrastructure", asof="2026-08-31", historical_mode=False)
    assert old.row_fields["production_model_version"] == "v1"
    assert new.row_fields["production_model_version"] == "v2"


def test_manual_override_receipt_requires_explicit_gate_acknowledgement() -> None:
    receipt: dict[str, object] = {
        "schema_version": MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION,
        "decision_type": "manual_economic_override",
        "model_family": "software_infrastructure",
        "model_version": "software_v2",
        "effective_date": "2026-08-31",
        "approved_by": "user",
        "stage8_run_id": "stage8",
        "walk_forward_run_id": "walk_forward",
        "config_sha256": "c",
        "signal_panel_sha256": "p",
        "weights_sha256": "w",
        "strict_gate_failure_acknowledged": 1,
        "consolidated_decision_sha256": "d",
        "rollback_weights_sha256": "r",
        "rollback_scoring_config_key": "rollback",
        "probation_contract": {"required_trading_sessions": 21},
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    assert validate_promotion_receipt(receipt, model_family="software_infrastructure") == []
    receipt["strict_gate_failure_acknowledged"] = 0
    assert "Manual promotion receipt must acknowledge strict gate failure." in validate_promotion_receipt(
        receipt, model_family="software_infrastructure"
    )


def test_run_manifest_is_json_serializable(tmp_path: Path) -> None:
    # Guards against accidental Path/datetime values entering the public schema.
    output = tmp_path / "out"
    output.mkdir()
    config = tmp_path / "config.yaml"
    panel = tmp_path / "panel.csv"
    config.write_text("x: 1\n", encoding="utf-8")
    panel.write_text("x\n1\n", encoding="utf-8")
    (output / "a.json").write_text("{}\n", encoding="utf-8")
    manifest = seal_calibration_run(
        output_dir=output,
        manifest_filename="manifest.json",
        run_id="fixed-test-run",
        model_family="semiconductors",
        stage="stage8",
        config_path=config,
        panel_path=panel,
        artifact_names=["a.json"],
    )
    json.dumps(manifest)

def test_semiconductor_stage8_uses_separate_immutable_panel_evidence(tmp_path: Path) -> None:
    config = tmp_path / "technology.yaml"
    output = tmp_path / "optuna"
    output.mkdir()
    config.write_text(
        "semiconductor_optuna_calibration:\n"
        "  robustness_folds: 2\n"
        "semiconductor_signal_diagnostics:\n"
        "  horizons_trading_days: [21, 63]\n"
        "  step_trading_days: 21\n",
        encoding="utf-8",
    )

    def write_csv(name: str, rows: list[dict[str, object]]) -> None:
        path = output / name
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    started_ns = time.time_ns()
    summary_rows: list[dict[str, object]] = [
        {
            "model": "stage7_baseline",
            "holdout_objective": 0.0,
            "holdout_nonzero_subfeatures": 1,
        },
        {
            "model": "stage8_best_candidate",
            "holdout_objective": 0.01,
            "holdout_nonzero_subfeatures": 1,
            "holdout_mean_ic_21": 0.02,
            "holdout_mean_ic_63": 0.02,
            "holdout_hit_rate_21": 1.0,
            "holdout_mean_spread_net_21": 0.01,
            "holdout_mean_spread_net_63": 0.01,
            "holdout_avg_top_turnover": 0.2,
            "holdout_avg_top_cohort_share": 0.2,
        },
    ]
    date_rows: list[dict[str, object]] = [
        {
            "asof_date": asof,
            "horizon_days": horizon,
            "ic": 0.02,
            "q5_minus_q1_fwd_resid": 0.01,
            "top_turnover": 0.2,
            "top_max_cohort_share": 0.2,
        }
        for asof in ("2024-01-02", "2024-02-01")
        for horizon in (21, 63)
    ]
    write_csv("stage8_trials.csv", [{"number": 0, "value": 0.01}])
    write_csv("stage8_best_summary.csv", summary_rows)
    write_csv("stage8_best_train_by_date.csv", date_rows)
    write_csv("stage8_best_holdout_by_date.csv", date_rows)
    write_csv("stage8_stage7_holdout_by_date.csv", date_rows)
    write_csv("stage8_stage7_full_by_date.csv", date_rows)
    write_csv("stage8_fold_robustness.csv", [{"fold": 0}])
    write_csv("stage8_candidate_current_scores.csv", [{"ticker": "TEST", "final_score": 50.0}])
    (output / "stage8_best_weights.json").write_text(
        json.dumps({"config_sha256": sha256_file(config), "post_lock_data_included": False}),
        encoding="utf-8",
    )

    manifest = harden_stage8(
        "semiconductors",
        config_path=config,
        output_dir=output,
        native_run_started_ns=started_ns,
    )
    panel = output / "stage8_panel_evidence.csv"
    governed_full = output / "stage8_stage7_full_by_date.csv"
    assert panel.exists()
    assert "calibration_run_id" not in panel.read_text(encoding="utf-8").splitlines()[0]
    assert "calibration_run_id" in governed_full.read_text(encoding="utf-8").splitlines()[0]
    assert manifest["signal_panel_sha256"] == sha256_file(panel)
    assert validate_stage8("semiconductors", config_path=config, output_dir=output) == []

