from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from consumer_defensive.core.calibration_v2 import build_calibration_decision
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    framework_sha256,
    load_framework,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "consumer_defensive/scripts/26_validate_consumer_defensive_promotion_framework_v2.py"
FRAMEWORK_PATH = ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml"


def _load_validator():
    spec = importlib.util.spec_from_file_location("consumer_defensive_file_validator_v2", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _value_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _seal(payload: dict[str, Any]) -> dict[str, Any]:
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def _performance() -> dict[str, float | int]:
    return {
        "paired_net_alpha_lcb": -0.01,
        "net_alpha_mean": -0.01,
        "absolute_profit_factor": 0.5,
        "relative_profit_factor": 0.5,
        "robust_profit_factor": 0.5,
        "deflated_sharpe_ratio": 0.0,
        "probability_of_backtest_overfitting": 1.0,
        "maximum_drawdown": 0.5,
        "expected_shortfall_95": -0.2,
        "turnover": 3.0,
        "average_transaction_cost": 0.02,
        "liquidity_capacity_ratio": 0.5,
        "winner_concentration_hhi": 0.3,
        "maximum_single_name_weight": 0.3,
        "paired_observation_count": 30,
        "positive_return_count": 1,
        "negative_return_count": 1,
    }


def _evidence(horizon: int, *, candidate_sha: str) -> dict[str, Any]:
    return {
        "evaluation_role": "outer_test",
        "horizon_sessions": horizon,
        "observation_count": 30,
        "observation_ids_sha256": "1" * 64,
        "fold_ids_sha256": "2" * 64,
        "signal_start_date": "2023-01-31",
        "signal_end_date": "2024-01-31",
        "latest_label_completion_date": "2024-07-31",
        "candidate_matrix_sha256": candidate_sha,
        "selected_weights_sha256": "3" * 64,
        "realized_return_stream_sha256": "4" * 64,
        "realized_return_count": 1,
        "realized_return_start_date": "2024-02-02",
        "realized_return_end_date": "2024-02-02",
    }


def _path_row(cohort: str, horizon: int) -> dict[str, Any]:
    gross = 1.01 / 1.0 - 1.0
    cost = 0.002
    identity = f"{cohort}:{horizon}"
    return {
        "observation_id": f"path:{identity}",
        "source_portfolio_observation_id": f"portfolio:{identity}",
        "fold_id": "outer_01",
        "cohort": cohort,
        "horizon_sessions": horizon,
        "signal_date": "2024-01-31",
        "entry_date": "2024-02-01",
        "return_date": "2024-02-02",
        "prior_nav": 1.0,
        "current_nav": 1.01,
        "entry_cash_value": 0.0,
        "cash_value": 0.0,
        "market_exposure_value": 1.01,
        "gross_exposure_ratio": 1.0,
        "gross_return": gross,
        "transaction_cost": cost,
        "net_return": gross - cost,
        "positions": [
            {
                "ticker": "AAA",
                "units": 0.01,
                "prior_mark": 100.0,
                "current_mark": 101.0,
                "prior_value": 1.0,
                "current_value": 1.01,
                "prior_provenance": "observed",
                "current_provenance": "observed",
                "prior_cash_component": 0.0,
                "current_cash_component": 0.0,
                "prior_market_component": 100.0,
                "current_market_component": 101.0,
                "terminal_event_sha256": "",
            }
        ],
    }


def _build_evidence(root: Path) -> dict[str, dict[str, Any]]:
    framework = load_framework(FRAMEWORK_PATH)
    asof = "2024-12-31"
    preregistration_sha = "a" * 64
    candidate_registry_sha = "b" * 64
    code_sha = "c" * 64
    candidate_matrix_sha = "d" * 64
    cohort_results = {
        cohort: {
            str(horizon): {
                "performance": _performance(),
                "evidence": _evidence(horizon, candidate_sha=candidate_matrix_sha),
            }
            for horizon in REQUIRED_HORIZONS
        }
        for cohort in REQUIRED_COHORTS
    }
    input_manifest = _seal(
        {
            "schema_version": "consumer_defensive_calibration_input_manifest_v2",
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "preregistration_sha256": preregistration_sha,
            "realized_path_policy": copy.deepcopy(VALIDATOR.EXPECTED_PATH_POLICY),
        }
    )
    path_rows = {
        cohort: {
            str(horizon): [_path_row(cohort, horizon)] for horizon in REQUIRED_HORIZONS
        }
        for cohort in REQUIRED_COHORTS
    }
    path_attestation = _seal(
        {
            "schema_version": "consumer_defensive_calibration_realized_path_attestation_v2",
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "preregistration_sha256": preregistration_sha,
            "path_policy": copy.deepcopy(VALIDATOR.EXPECTED_PATH_POLICY),
            "cohorts": path_rows,
        }
    )
    benchmark_attestation = _seal(
        {
            "schema_version": "consumer_defensive_matched_benchmark_attestation_v3",
            "model_family": "consumer_defensive",
            "primary_benchmark": "point_in_time_equal_weight_cohort",
            "diagnostic_benchmarks": ["XLP", "SPY"],
            "membership_sha256": "9" * 64,
            "cohorts": {
                cohort: {
                    str(horizon): [{
                        "signal_date": path_rows[cohort][str(horizon)][0]["signal_date"],
                        "prior_date": "2024-02-01",
                        "return_date": path_rows[cohort][str(horizon)][0]["return_date"],
                        "strategy_observation_id": path_rows[cohort][str(horizon)][0]["observation_id"],
                        "strategy_net_return": path_rows[cohort][str(horizon)][0]["net_return"],
                        "peer_weighting": "point_in_time_equal_weight_daily_rebalanced",
                        "peer_count": 2,
                        "peer_rows": [
                            {"ticker": "AAA", "prior_mark": 100.0, "current_mark": 101.0, "return": 0.01},
                            {"ticker": "BBB", "prior_mark": 200.0, "current_mark": 202.0, "return": 0.01},
                        ],
                        "primary_benchmark_return": 0.01,
                        "xlp_marks": {"prior_mark": 100.0, "current_mark": 100.5},
                        "xlp_return": 0.005,
                        "spy_marks": {"prior_mark": 200.0, "current_mark": 201.0},
                        "spy_return": 0.005,
                    }]
                    for horizon in REQUIRED_HORIZONS
                }
                for cohort in REQUIRED_COHORTS
            },
        }
    )
    fold_registry = _seal(
        {
            "schema_version": "consumer_defensive_calibration_fold_registry_v2",
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "preregistration_sha256": preregistration_sha,
            "realized_path_attestation_sha256": path_attestation["payload_sha256"],
            "matched_benchmark_attestation_sha256": benchmark_attestation[
                "payload_sha256"
            ],
            "cohorts": {
                cohort: {
                    str(horizon): {
                        "candidate_matrix_sha256": candidate_matrix_sha,
                        "realized_path_attestation_sha256": _value_sha(
                            path_rows[cohort][str(horizon)]
                        ),
                        "realized_daily_return_count": 1,
                    }
                    for horizon in REQUIRED_HORIZONS
                }
                for cohort in REQUIRED_COHORTS
            },
        }
    )
    decision = build_calibration_decision(
        asof_date=date.fromisoformat(asof),
        framework=framework,
        horizon_results_by_cohort=cohort_results,
        input_panel_sha256=input_manifest["payload_sha256"],
        fold_registry_sha256=fold_registry["payload_sha256"],
        candidate_registry_sha256=candidate_registry_sha,
        code_sha256=code_sha,
    )
    results = _seal(
        {
            "schema_version": "consumer_defensive_calibration_results_v2",
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "preregistration_sha256": preregistration_sha,
            "candidate_registry_sha256": candidate_registry_sha,
            "input_manifest_sha256": input_manifest["payload_sha256"],
            "fold_registry_sha256": fold_registry["payload_sha256"],
            "realized_path_attestation_sha256": path_attestation["payload_sha256"],
            "matched_benchmark_attestation_sha256": benchmark_attestation[
                "payload_sha256"
            ],
            "cohort_horizon_results": cohort_results,
            "decision_payload_sha256": decision["payload_sha256"],
            "production_promotion_enabled": False,
            "portfolio_write_enabled": False,
        }
    )
    execution_validation = _seal(
        {
            "schema_version": "consumer_defensive_calibration_independent_validation_v2",
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "status": "PASS",
            "framework_sha256": framework_sha256(framework),
            "decision_payload_sha256": decision["payload_sha256"],
            "input_manifest_sha256": input_manifest["payload_sha256"],
            "fold_registry_sha256": fold_registry["payload_sha256"],
            "realized_path_attestation_sha256": path_attestation["payload_sha256"],
            "matched_benchmark_attestation_sha256": benchmark_attestation[
                "payload_sha256"
            ],
            "candidate_registry_sha256": candidate_registry_sha,
            "code_sha256": code_sha,
            "decision_sequence": 1,
            "production_write_performed": False,
            "portfolio_write_performed": False,
        }
    )
    payloads = {
        "input_manifest": input_manifest,
        "fold_registry": fold_registry,
        "path_attestation": path_attestation,
        "benchmark_attestation": benchmark_attestation,
        "results": results,
        "decision": decision,
        "execution_validation": execution_validation,
    }
    root.mkdir(parents=True)
    for label, filename in VALIDATOR.EVIDENCE_FILES.items():
        (root / filename).write_text(
            json.dumps(payloads[label], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return payloads


def test_separate_file_validator_accepts_exact_bound_evidence(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    _build_evidence(evidence_root)
    report = VALIDATOR.validate_evidence_root(
        evidence_root,
        framework=load_framework(FRAMEWORK_PATH),
    )
    assert report["status"] == "PASS"
    assert report["realized_path_row_count"] == 12
    assert report["production_write_performed"] is False
    assert "calibration_execution_v2" not in SCRIPT.read_text(encoding="utf-8")


def test_cross_hash_and_self_hash_tampering_fail_closed(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    payloads = _build_evidence(evidence_root)
    results = payloads["results"]
    results["candidate_registry_sha256"] = "e" * 64
    _seal(results)
    (evidence_root / VALIDATOR.EVIDENCE_FILES["results"]).write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cross-hash binding"):
        VALIDATOR.validate_evidence_root(evidence_root, framework=load_framework(FRAMEWORK_PATH))

    payloads = _build_evidence(tmp_path / "evidence_self_hash")
    input_manifest = payloads["input_manifest"]
    input_manifest["asof_date"] = "2024-12-30"
    (tmp_path / "evidence_self_hash" / VALIDATOR.EVIDENCE_FILES["input_manifest"]).write_text(
        json.dumps(input_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="self-hash mismatch"):
        VALIDATOR.validate_evidence_root(
            tmp_path / "evidence_self_hash",
            framework=load_framework(FRAMEWORK_PATH),
        )


def test_independent_path_arithmetic_rejects_rehashed_position_tamper(tmp_path: Path) -> None:
    payloads = _build_evidence(tmp_path / "evidence")
    path = payloads["path_attestation"]
    fold = payloads["fold_registry"]
    cohort = sorted(REQUIRED_COHORTS)[0]
    row = path["cohorts"][cohort]["21"][0]
    row["positions"][0]["current_value"] = 1.02
    fold["cohorts"][cohort]["21"]["realized_path_attestation_sha256"] = _value_sha(
        path["cohorts"][cohort]["21"]
    )
    with pytest.raises(ValueError, match="current position value"):
        VALIDATOR._validate_realized_path(path, fold_registry=fold)
