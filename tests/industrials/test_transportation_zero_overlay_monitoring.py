from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from industrials.transportation.zero_overlay_monitoring import (
    MONITORING_VERSION,
    SOURCE_FIELDS,
    audit_monitoring_state,
    capture_signal_snapshot,
    load_monitoring_policy,
    signal_paths,
)
from industrials.transportation.financial_contract import MetricDefinition
from industrials.transportation.monitoring_source import build_source_rows
from industrials.transportation.scoring import COMPONENT_FIELD
from industrials.transportation.selected_feature_history import sha256


def _write_policy(
    path: Path,
    *,
    origin_hash: str = "a" * 64,
) -> None:
    path.write_text(
        "\n".join(
            (
                "model_family: transportation",
                f"policy_version: {MONITORING_VERSION}",
                "origin_gate: DP15_FINALIZE_ZERO_OVERLAY_PORTFOLIO_SHADOW",
                'origin_asof_date: "2026-07-22"',
                f"origin_dp15_sha256: {origin_hash}",
                'first_signal_date: "2026-07-31"',
                'earliest_outcome_review_date: "2027-09-30"',
                "signal_cadence: month_end_post_refresh_companion",
                "forward_horizon_trading_days: 63",
                "minimum_new_monthly_signals_per_candidate: 12",
                "minimum_cross_section_per_candidate: 3",
                "outcome_access_during_capture: prohibited",
                "optimizer_during_monitoring: disabled",
                "production_promotion_during_monitoring: prohibited",
                "historical_rebuild_during_monitoring: prohibited",
                "portfolio_overlay_weights:",
                "  fleet_utilization: 0.0",
                "  operating_ratio: 0.0",
                "  passenger_load_factor: 0.0",
                "research_challenger_weights:",
                "  fleet_utilization: 0.10",
                "  operating_ratio: 0.10",
                "  passenger_load_factor: 0.10",
                "candidate_cohorts:",
                "  fleet_utilization: marine",
                "  operating_ratio: surface",
                "  passenger_load_factor: air",
                "candidate_directions:",
                "  fleet_utilization: 1",
                "  operating_ratio: -1",
                "  passenger_load_factor: 1",
                "",
            )
        ),
        encoding="utf-8",
    )


def _write_dp15(path: Path, calibration_path: Path) -> None:
    calibration_path.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "validation_selected_weights": {
                    "fleet_utilization": 0.1,
                    "operating_ratio": 0.1,
                    "passenger_load_factor": 0.1,
                },
            }
        ),
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "gate": "DP15_FINALIZE_ZERO_OVERLAY_PORTFOLIO_SHADOW",
                "asof_date": "2026-07-22",
                "zero_overlay_decision_sealed": True,
                "production_promotion_authorized": False,
                "final_research_weights": {
                    "fleet_utilization": 0.0,
                    "operating_ratio": 0.0,
                    "passenger_load_factor": 0.0,
                },
                "artifacts": {
                    "calibration_validation": {
                        "path": str(calibration_path.resolve()),
                        "sha256": sha256(calibration_path),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_source(path: Path, asof: str) -> None:
    cohorts = {
        "fleet_utilization": "marine",
        "operating_ratio": "surface",
        "passenger_load_factor": "air",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_FIELDS)
        writer.writeheader()
        for metric, cohort in cohorts.items():
            for index, ticker in enumerate(("A", "B", "C"), start=1):
                writer.writerow(
                    {
                        "asof_date": asof,
                        "ticker": f"{metric[:2]}{ticker}",
                        "metric_id": metric,
                        "calibration_cohort": cohort,
                        "baseline_score": 40 + index,
                        "specialized_percentile": 20 * index,
                    }
                )


def test_policy_rejects_nonzero_portfolio_weight(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    _write_policy(policy)
    text = policy.read_text(encoding="utf-8").replace(
        "fleet_utilization: 0.0",
        "fleet_utilization: 0.01",
        1,
    )
    policy.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="portfolio weights"):
        load_monitoring_policy(policy)


def test_capture_is_outcome_blind_and_idempotent(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    source = tmp_path / "source.csv"
    output = tmp_path / "output"
    _write_policy(policy)
    _write_source(source, "2026-07-31")
    first = capture_signal_snapshot(
        asof="2026-07-31",
        source_snapshot=source,
        policy_path=policy,
        output_root=output,
    )
    second = capture_signal_snapshot(
        asof="2026-07-31",
        source_snapshot=source,
        policy_path=policy,
        output_root=output,
    )
    assert first["acceptance"] == "PASS"
    assert second == first
    assert first["row_count"] == 9
    assert first["outcomes_accessed"] is False
    signal, manifest = signal_paths(output, "2026-07-31")
    assert signal.is_file() and manifest.is_file()
    assert "return" not in signal.read_text(encoding="utf-8").splitlines()[0]


def test_capture_rejects_pre_freeze_date(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    source = tmp_path / "source.csv"
    _write_policy(policy)
    _write_source(source, "2026-07-22")
    with pytest.raises(ValueError, match="precedes frozen start"):
        capture_signal_snapshot(
            asof="2026-07-22",
            source_snapshot=source,
            policy_path=policy,
            output_root=tmp_path / "output",
        )


def test_monitor_waits_without_new_signals(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    dp15 = tmp_path / "dp15.json"
    calibration = tmp_path / "calibration.json"
    _write_dp15(dp15, calibration)
    _write_policy(policy, origin_hash=sha256(dp15))
    result = audit_monitoring_state(
        asof="2026-07-29",
        policy_path=policy,
        dp15_path=dp15,
        output_root=tmp_path / "monitor",
    )
    assert result["acceptance"] == "PASS"
    assert result["history_gate_pass"] is False
    assert result["ready_for_separate_outcome_audit"] is False
    assert result["outcomes_accessed"] is False
    assert result["recalibration_authorized"] is False
    assert result["next_gate"] == "CONTINUE_ZERO_OVERLAY_SHADOW_MONITORING"


def test_source_export_rebuilds_baseline_and_directional_percentiles() -> None:
    asof = "2026-07-31"
    policy = {
        "candidate_cohorts": {
            "fleet_utilization": "marine",
            "operating_ratio": "surface",
            "passenger_load_factor": "air",
        },
        "candidate_directions": {
            "fleet_utilization": 1,
            "operating_ratio": -1,
            "passenger_load_factor": 1,
        },
        "minimum_cross_section_per_candidate": 3,
    }
    definition = MetricDefinition(
        metric_id="generic_quality",
        component="quality",
        source="test",
        source_field="",
        formula="",
        candidate_metric="",
        direction=1,
        cohorts=("*",),
        industries=(),
        required_for_rank=False,
        specialized=False,
        unit="ratio",
        minimum_history_days=0,
        winsor_lower=0.0,
        winsor_upper=1.0,
        birthdate="",
        production_status="active",
    )
    weights = {component: 0.0 for component in COMPONENT_FIELD}
    weights["quality"] = 1.0
    rows: list[dict[str, str]] = []
    for metric, cohort in policy["candidate_cohorts"].items():
        for index in range(1, 4):
            ticker = f"{metric[:2].upper()}{index}"
            rows.extend(
                [
                    {
                        "asof_date": asof,
                        "ticker": ticker,
                        "metric_id": "generic_quality",
                        "metric_family": "generic",
                        "source_lane": "V2_GENERIC",
                        "calibration_cohort": cohort,
                        "industry": "Transportation",
                        "availability_status": "REPORTED",
                        "metric_value": str(index),
                    },
                    {
                        "asof_date": asof,
                        "ticker": ticker,
                        "metric_id": metric,
                        "metric_family": "specialized",
                        "source_lane": "V3_SPECIALIZED",
                        "calibration_cohort": cohort,
                        "industry": "Transportation",
                        "availability_status": "REPORTED",
                        "metric_value": str(index),
                    },
                ]
            )

    exported = build_source_rows(
        rows,
        asof=asof,
        policy=policy,
        definitions=[definition],
        component_weights=weights,
    )

    assert len(exported) == 9
    operating = {
        row["ticker"]: float(row["specialized_percentile"])
        for row in exported
        if row["metric_id"] == "operating_ratio"
    }
    assert operating == {"OP1": 100.0, "OP2": 50.0, "OP3": 0.0}
    assert all(set(row) == set(SOURCE_FIELDS) for row in exported)
