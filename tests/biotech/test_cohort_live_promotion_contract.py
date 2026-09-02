from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from biotech_index.core.cohort_calibration import BIOTECH_CALIBRATION_COHORTS
from biotech_index.core.promotion_contract import (
    ActiveCohortPromotion,
    ActivePromotionContract,
    PromotionContractError,
    load_active_promotion_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, module_name: str) -> ModuleType:
    path = PROJECT_ROOT / "biotech_index" / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def score_spec() -> dict[str, object]:
    return {
        "candidate_name": "cohort_candidate",
        "description": "cohort formula",
        "clinical_catalyst": 0.40,
        "clinical_credibility": 0.25,
        "clinical_financial_quality": 0.20,
        "clinical_momentum": 0.15,
        "clinical_risk_penalty": 0.20,
        "clinical_stage_profile": {
            "clinical_opportunity": 0.60,
            "financial_quality": 0.40,
            "risk_penalty": 0.20,
        },
        "commercial_stage_profile": {
            "commercial_value": 0.70,
            "financial_quality": 0.30,
            "risk_penalty": 0.15,
        },
    }


def fold_contract(*, candidate_id: str = "candidate-a") -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_pool_top_n": 20,
        "candidate_spec": score_spec(),
        "selection_policy": {"policy_name": "core_structural_veto"},
        "threshold": {
            "min_score_pct_of_top": 85.0,
            "max_names": 2,
            "reliability_class": "medium",
            "active_weight": 0.60,
        },
        "outer_test_comparison_row": {"paired_end_date": "2026-08-21"},
    }


def cohort_payload(*, active: bool = True) -> dict[str, object]:
    promoted = BIOTECH_CALIBRATION_COHORTS[0]
    statuses: dict[str, dict[str, object]] = {}
    contracts: dict[str, dict[str, object]] = {}
    for cohort in BIOTECH_CALIBRATION_COHORTS:
        authorized = cohort == promoted
        statuses[cohort] = {
            "cohort_promotion_authorized": authorized,
            "candidate_id": "candidate-a" if authorized else "production_incumbent_fallback",
            "selection_policy_name": "core_structural_veto",
        }
        contracts[cohort] = {
            "latest_primary_fold_contract": (
                fold_contract()
                if authorized
                else {
                    **fold_contract(candidate_id="production_incumbent_fallback"),
                    "candidate_spec": score_spec(),
                }
            )
        }
    return {
        "contract_version": "biotech_cohort_promotion_contract_v1",
        "contract_id": "biotech-2026-08-31-cohort-suite-abcdef123456",
        "activation_status": "active" if active else "candidate_requires_explicit_activation",
        "effective_date": "2026-08-31",
        "production_promotion_authorized": True,
        "global_portfolio_risk_gate_passed": True,
        "cohort_budget_weights": {cohort: 0.20 for cohort in BIOTECH_CALIBRATION_COHORTS},
        "cohort_promotion_status": statuses,
        "cohort_contracts": contracts,
        "statistically_and_economically_authorized_cohorts": [promoted],
        "global_portfolio_profitability_decision": {"profitability_promotion_authorized": True},
        "profitability_replay_verification": {
            "verification_status": "pass",
            "independent_normalized_input_replay": True,
        },
        "monitoring_contract": {
            "review_windows_days": [30, 60, 90],
            "rollback_triggers": {
                "min_live_paired_dates": 20,
                "max_loss20_deterioration_pct": 2.0,
                "max_loss40_deterioration_pct": 1.0,
                "max_policy_fallback_frequency_pct": 25.0,
                "max_drawdown_deterioration_pct": 5.0,
                "max_daily_cvar_deterioration_pct": 0.5,
                "require_policy_hash_consistency": True,
            },
            "rollback_action": "xbi_residual_only",
        },
    }


def write_payload(path: Path, payload: dict[str, object]) -> str:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_active_cohort_contract_loads_only_independently_authorized_cohort(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    sha256 = write_payload(path, cohort_payload())
    config: dict[str, Any] = {
        "biotech_scoring": {
            "adaptive_promotion_contract": {"enabled": True, "path": str(path), "sha256": sha256}
        }
    }

    contract = load_active_promotion_contract(config, base_dir=tmp_path)

    assert contract is not None
    assert set(contract.cohort_policies) == {BIOTECH_CALIBRATION_COHORTS[0]}
    promotion = contract.policy_for_cohort(BIOTECH_CALIBRATION_COHORTS[0])
    assert promotion is not None
    assert promotion.max_names == 2
    assert promotion.active_weight == pytest.approx(0.60)
    assert contract.policy_for_cohort(BIOTECH_CALIBRATION_COHORTS[1]) is None


def test_cohort_contract_rejects_authorization_metadata_drift(tmp_path: Path) -> None:
    path = tmp_path / "drift.json"
    payload = cohort_payload()
    statuses = payload["cohort_promotion_status"]
    assert isinstance(statuses, dict)
    second = statuses[BIOTECH_CALIBRATION_COHORTS[1]]
    assert isinstance(second, dict)
    second["cohort_promotion_authorized"] = True
    sha256 = write_payload(path, payload)
    config: dict[str, Any] = {
        "biotech_scoring": {
            "adaptive_promotion_contract": {"enabled": True, "path": str(path), "sha256": sha256}
        }
    }

    with pytest.raises(PromotionContractError, match="does not match"):
        load_active_promotion_contract(config, base_dir=tmp_path)


def test_cohort_live_gate_changes_only_promoted_cohort(tmp_path: Path) -> None:
    scorer = load_script("11_score_biotech_index.py", "biotech_cohort_contract_scorer_test")
    promoted = BIOTECH_CALIBRATION_COHORTS[0]
    untouched = BIOTECH_CALIBRATION_COHORTS[1]
    promotion = ActiveCohortPromotion(
        cohort=promoted,
        candidate_id="candidate-a",
        candidate_name="cohort_candidate",
        selection_policy_name="core_structural_veto",
        candidate_pool_top_n=20,
        min_score_pct_of_top=85.0,
        max_names=2,
        reliability_class="medium",
        active_weight=0.60,
        xbi_residual_weight=0.40,
        policy_payload={"policy_name": "core_structural_veto"},
        score_spec=score_spec(),
    )
    contract = ActivePromotionContract(
        path=tmp_path / "active.json",
        sha256="a" * 64,
        effective_date=date(2026, 8, 31),
        contract_id="contract-a",
        candidate_id="cohort_specific",
        candidate_name="cohort_specific_promotions",
        selection_policy_name="cohort_specific",
        candidate_pool_top_n=0,
        min_score_pct_of_top=0.0,
        max_names=0,
        reliability_class="cohort_specific",
        active_weight=1.0,
        xbi_residual_weight=0.0,
        policy_payload={},
        contract_version="biotech_cohort_promotion_contract_v1",
        cohort_policies={promoted: promotion},
    )

    def row(ticker: str, cohort: str, score: float, *, gate: float = 0.0) -> dict[str, object]:
        return {
            "ticker": ticker,
            "asof_date": "2026-09-01",
            "biotech_primary_cohort": cohort,
            "native_score_value": score,
            "opportunity_score": score,
            "portfolio_candidate_gate": gate,
            "portfolio_candidate_status": "eligible" if gate else "excluded",
            "portfolio_candidate_reason": "incumbent_eligible" if gate else "incumbent_excluded",
            "eligibility_reason": "incumbent_eligible" if gate else "incumbent_excluded",
            "score_zero_is_missing_flag": 0.0,
            "biotech_cohort_investible_flag": 1.0,
            "core_structural_veto_flag": 0.0,
            "rank_quality_cap_vetoed": 0.0,
            "price_data_asof_date": "2026-09-01",
            "universe_status": "live",
        }

    rows = [
        row("A1", promoted, 100.0),
        row("A2", promoted, 90.0),
        row("A3", promoted, 70.0),
        row("B1", untouched, 80.0, gate=1.0),
    ]

    scorer.apply_promoted_portfolio_candidate_policy(rows, {}, active_contract=contract)

    by_ticker = {str(item["ticker"]): item for item in rows}
    assert by_ticker["A1"]["portfolio_candidate_gate"] == 1.0
    assert by_ticker["A2"]["portfolio_candidate_gate"] == 1.0
    assert by_ticker["A3"]["portfolio_candidate_gate"] == 0.0
    assert by_ticker["B1"]["portfolio_candidate_gate"] == 1.0
    assert by_ticker["B1"]["portfolio_candidate_reason"] == "incumbent_eligible"
    # Two selected names at the contract's default 25% name cap can support
    # only 50% active exposure, despite the nominal 60% reliability weight.
    assert by_ticker["A1"]["biotech_active_sleeve_weight"] == pytest.approx(0.50)
    assert by_ticker["A1"]["biotech_xbi_residual_weight"] == pytest.approx(0.50)
    assert by_ticker["A1"]["biotech_max_name_weight_within_cohort"] == pytest.approx(0.25)
    assert by_ticker["A1"]["biotech_selected_name_count_within_cohort"] == 2.0
    assert "biotech_promotion_contract_id" not in by_ticker["B1"]


def test_activation_evidence_date_reads_deployable_row_for_all_cohorts() -> None:
    activation = load_script("61_activate_biotech_promotion_contract.py", "biotech_cohort_activation_test")
    payload = cohort_payload(active=False)
    contracts = payload["cohort_contracts"]
    assert isinstance(contracts, dict)
    latest = contracts[BIOTECH_CALIBRATION_COHORTS[-1]]
    assert isinstance(latest, dict)
    fold = latest["latest_primary_fold_contract"]
    assert isinstance(fold, dict)
    comparison = fold["outer_test_comparison_row"]
    assert isinstance(comparison, dict)
    comparison["paired_end_date"] = "2026-08-22"

    assert activation.evidence_end_date(payload).isoformat() == "2026-08-22"
