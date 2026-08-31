from __future__ import annotations

import json
from pathlib import Path

import pytest

from biotech_index.core.promotion_contract import (
    PromotionContractError,
    load_active_promotion_contract,
    sha256_file,
)


def contract_config(path: Path, sha256: str) -> dict[str, object]:
    return {
        "biotech_scoring": {
            "weights": {
                "catalyst": 0.30,
                "credibility": 0.20,
                "financial_quality": 0.15,
                "momentum": 0.10,
                "risk_penalty": 0.25,
            },
            "investment_weight_profiles": {
                "clinical_stage": {"clinical_opportunity": 0.60, "financial_quality": 0.40},
                "commercial_stage": {"commercial_value": 0.70, "financial_quality": 0.30},
            },
            "production_baseline": {"selection_policy": "core_structural_veto"},
            "adaptive_promotion_contract": {
                "enabled": True,
                "path": str(path),
                "sha256": sha256,
            },
        }
    }


def contract_payload(*, policy_name: str = "core_structural_veto") -> dict[str, object]:
    return {
        "contract_version": "biotech_promotion_contract_v1",
        "contract_id": "biotech-2026-09-01-candidate-1",
        "activation_status": "active",
        "effective_date": "2026-09-01",
        "production_promotion_authorized": True,
        "monitoring_contract": {
            "review_windows_days": [30, 60, 90],
            "rollback_triggers": {
                "min_live_paired_dates": 20,
                "max_loss20_deterioration_pct": 2.0,
                "max_loss40_deterioration_pct": 1.0,
                "max_policy_fallback_frequency_pct": 25.0,
                "require_policy_hash_consistency": True,
            },
            "rollback_action": "xbi_residual_only",
        },
        "latest_primary_fold_contract": {
            "candidate_id": "candidate-1",
            "candidate_pool_top_n": 20,
            "candidate_spec": {
                "candidate_name": "candidate",
                "clinical_catalyst": 0.30,
                "clinical_credibility": 0.20,
                "clinical_financial_quality": 0.15,
                "clinical_momentum": 0.10,
                "clinical_risk_penalty": 0.25,
                "clinical_stage_profile": {
                    "clinical_opportunity": 0.60,
                    "financial_quality": 0.40,
                },
                "commercial_stage_profile": {
                    "commercial_value": 0.70,
                    "financial_quality": 0.30,
                },
            },
            "selection_policy": {"policy_name": policy_name},
            "threshold": {
                "min_score_pct_of_top": 85.0,
                "max_names": 8,
                "reliability_class": "medium",
                "active_weight": 0.55,
            },
        },
    }


def write_contract(path: Path, payload: dict[str, object]) -> str:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return sha256_file(path)


def test_active_contract_is_hash_pinned_and_score_policy_reproducible(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    sha256 = write_contract(path, contract_payload())
    contract = load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)
    assert contract is not None
    assert contract.contract_id == "biotech-2026-09-01-candidate-1"
    assert contract.candidate_pool_top_n == 20
    assert contract.max_names == 8
    assert contract.active_weight == pytest.approx(0.55)
    assert contract.xbi_residual_weight == pytest.approx(0.45)
    assert contract.effective_date.isoformat() == "2026-09-01"


def test_contract_rejects_policy_without_live_parity(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.json"
    sha256 = write_contract(path, contract_payload(policy_name="borrow_squeeze_anchored"))
    config = contract_config(path, sha256)
    config["biotech_scoring"]["production_baseline"]["selection_policy"] = "borrow_squeeze_anchored"  # type: ignore[index]
    with pytest.raises(PromotionContractError, match="no proven live scorer parity"):
        load_active_promotion_contract(config, base_dir=tmp_path)  # type: ignore[arg-type]


def test_contract_rejects_missing_monitoring_contract(tmp_path: Path) -> None:
    path = tmp_path / "missing-monitoring.json"
    payload = contract_payload()
    del payload["monitoring_contract"]
    sha256 = write_contract(path, payload)
    with pytest.raises(PromotionContractError, match="30-, 60-, and 90-day"):
        load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)


def test_contract_rejects_missing_immutable_contract_id(tmp_path: Path) -> None:
    path = tmp_path / "missing-contract-id.json"
    payload = contract_payload()
    del payload["contract_id"]
    sha256 = write_contract(path, payload)
    with pytest.raises(PromotionContractError, match="immutable contract_id"):
        load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)


def test_contract_rejects_fractional_monitoring_window_and_missing_tail_trigger(tmp_path: Path) -> None:
    path = tmp_path / "bad-monitoring.json"
    payload = contract_payload()
    monitoring = payload["monitoring_contract"]
    assert isinstance(monitoring, dict)
    monitoring["review_windows_days"] = [30.5, 60, 90]
    sha256 = write_contract(path, payload)
    with pytest.raises(PromotionContractError, match="positive whole days"):
        load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)

    payload = contract_payload()
    monitoring = payload["monitoring_contract"]
    assert isinstance(monitoring, dict)
    triggers = monitoring["rollback_triggers"]
    assert isinstance(triggers, dict)
    del triggers["max_loss40_deterioration_pct"]
    sha256 = write_contract(path, payload)
    with pytest.raises(PromotionContractError, match="max_loss40_deterioration_pct"):
        load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)


def test_contract_rejects_hash_drift(tmp_path: Path) -> None:
    path = tmp_path / "drift.json"
    sha256 = write_contract(path, contract_payload())
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(PromotionContractError, match="hash mismatch"):
        load_active_promotion_contract(contract_config(path, sha256), base_dir=tmp_path)
