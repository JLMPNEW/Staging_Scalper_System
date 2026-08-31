from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from industrials.transportation.future_oos_capture_v2 import validate_governing_contracts


def _contracts(tmp_path: Path) -> tuple[Path, Path]:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "policy_version": "transportation_subgroup_score_policy_v8",
                "model_version": "transportation_hierarchical_subgroup_score_v8",
                "effective_from": "2026-08-21",
                "evidence_class": "outcome_blind_economic_specification",
                "controls": {
                    "group_weights_use_outcomes": False,
                    "component_weights_use_outcomes": False,
                    "historical_results_can_authorize_production": False,
                },
                "governance": {
                    "cohort_promotion_independent": True,
                    "group_failure_cannot_be_hidden_by_aggregate_result": True,
                    "production_activation_authorized": False,
                },
            }
        ),
        encoding="utf-8",
    )
    decision = tmp_path / "decision.json"
    decision.write_text(
        json.dumps(
            {
                "production_activation_authorized": False,
                "research_specification": {
                    "contract_version": "transportation_v7_research_specification_v1",
                    "first_future_signal_date": "2026-08-24",
                    "promotion_gate": {
                        "minimum_future_21_session_non_overlapping_outcomes": 12,
                        "minimum_future_63_session_non_overlapping_outcomes": 4,
                        "minimum_ic": 0.0,
                        "minimum_top_minus_cohort_net": 0.0,
                        "minimum_top_minus_bottom_gross": 0.0,
                        "minimum_hit_rate": 0.55,
                        "cohort_isolation_required": True,
                        "independent_promotion_readiness_audit_required": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return policy, decision


def test_governing_v7_v8_contracts_are_semantically_bound(tmp_path: Path) -> None:
    policy, decision = _contracts(tmp_path)
    result = validate_governing_contracts(
        v8_policy_path=policy,
        v7_research_decision_path=decision,
    )
    assert len(result["v7_future_gate_sha256"]) == 64


def test_tampered_hit_threshold_is_rejected(tmp_path: Path) -> None:
    policy, decision = _contracts(tmp_path)
    payload = json.loads(decision.read_text(encoding="utf-8"))
    payload["research_specification"]["promotion_gate"]["minimum_hit_rate"] = 0.50
    decision.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="future gate changed"):
        validate_governing_contracts(
            v8_policy_path=policy,
            v7_research_decision_path=decision,
        )


def test_outcome_informed_weights_are_rejected(tmp_path: Path) -> None:
    policy, decision = _contracts(tmp_path)
    payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
    payload["controls"]["component_weights_use_outcomes"] = True
    policy.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="outcome-blind"):
        validate_governing_contracts(
            v8_policy_path=policy,
            v7_research_decision_path=decision,
        )
