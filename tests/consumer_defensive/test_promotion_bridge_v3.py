from __future__ import annotations

import copy

import pytest

from consumer_defensive.core.promotion_bridge_v3 import (
    BRIDGE_METHODOLOGY_PATHS,
    DESIGN_EVIDENCE_MAXIMUM_STATE,
    DESIGN_EVIDENCE_ROLE,
    SOURCE_ARTIFACT_NAMES,
    _validate_decision_bindings,
    _outer_oos_from_path,
    build_input_build_attestation,
    validate_capital_context_binding,
    validate_input_build_attestation,
)
from consumer_defensive.core.capital_context_v1 import canonical_payload_sha256
from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    value_sha256,
)


def _digest(character: str) -> str:
    return character * 64


def _attestation() -> dict:
    contracts = {
        cohort: {"payload_sha256": _digest("c")}
        for cohort in REQUIRED_COHORTS
    }
    return build_input_build_attestation(
        asof_date="2026-08-27",
        source_file_sha256s={name: _digest("a") for name in SOURCE_ARTIFACT_NAMES},
        source_payload_sha256s={
            name: _digest("b")
            for name in SOURCE_ARTIFACT_NAMES
            if name not in {"promotion_framework_v2", "promotion_framework_v3"}
        },
        source_calibration_code_sha256=_digest("d"),
        bridge_methodology_file_sha256s={
            path: _digest("e") for path in BRIDGE_METHODOLOGY_PATHS
        },
        production_model_contracts=contracts,
        benchmark_attestation_sha256=_digest("f"),
        promotion_input_sha256=_digest("1"),
        capital_context_asof_date="2026-08-28",
        capital_context_file_sha256=_digest("2"),
        trusted_capital_context_file_sha256=_digest("2"),
        capital_context_payload_sha256=_digest("3"),
        normalized_capital_context_payload_sha256=_digest("4"),
    )


def _portfolio_capital_context(*, asof_date: str = "2026-08-28") -> dict:
    payload = {
        "schema_version": "portfolio_capital_context_v1",
        "authority_owner": "portfolio_layer",
        "artifact_role": "report_only_capital_context",
        "allocation_basis": "explicit_fraction_of_account_aum",
        "asof_date": asof_date,
        "account_aum_usd": "500000.00",
        "active_sector_count": 8,
        "equal_split_reference": {"numerator": 1, "denominator": 8},
        "sector_cap_fraction": "0.125",
        "sector_cap_notional_usd": "62500.00",
        "source_id": "user_confirmed_planning_aum",
        "source_sha256": _digest("9"),
        "portfolio_write_performed": False,
    }
    payload["payload_sha256"] = canonical_payload_sha256(payload)
    return payload


def test_build_attestation_is_design_only_database_free_and_self_sealed() -> None:
    payload = _attestation()
    assert payload["evidence_role"] == DESIGN_EVIDENCE_ROLE
    assert payload["maximum_authorized_state"] == DESIGN_EVIDENCE_MAXIMUM_STATE
    assert payload["maximum_authorized_state"] == "active_full"
    assert payload["database_read_performed"] is False
    assert payload["database_write_performed"] is False
    assert payload["portfolio_write_performed"] is False
    assert payload["capital_context_asof_date"] == "2026-08-28"
    assert payload["capital_context_file_sha256"] == _digest("2")
    assert payload["capital_context_payload_sha256"] == _digest("3")
    assert payload["normalized_capital_context_payload_sha256"] == _digest("4")
    assert payload["capital_context_counts_as_fresh_predictive_evidence"] is False
    assert validate_input_build_attestation(payload) == payload


def test_capital_context_binding_normalizes_exact_account_and_budget() -> None:
    raw, normalized = validate_capital_context_binding(
        portfolio_capital_context=_portfolio_capital_context(),
        capital_context_file_sha256=_digest("5"),
        trusted_capital_context_file_sha256=_digest("5"),
        evidence_asof_date="2026-08-27",
        calibration_reference_notional_usd=1_000_000.0,
    )
    assert raw["payload_sha256"] == _portfolio_capital_context()["payload_sha256"]
    assert normalized["asof_date"] == "2026-08-28"
    assert normalized["account_aum_usd"] == 500_000.0
    assert normalized["active_sector_count"] == 8
    assert normalized["sector_max_fraction"] == 0.125
    assert normalized["sector_max_notional_usd"] == 62_500.0
    assert normalized["calibration_reference_notional_usd"] == 1_000_000.0
    assert (
        normalized["capacity_test_basis"]
        == "full_consumer_defensive_sector_budget_per_cohort_conservative"
    )


def test_capital_context_binding_rejects_wrong_file_pin_and_stale_context() -> None:
    with pytest.raises(ValueError, match="trusted digest"):
        validate_capital_context_binding(
            portfolio_capital_context=_portfolio_capital_context(),
            capital_context_file_sha256=_digest("5"),
            trusted_capital_context_file_sha256=_digest("6"),
            evidence_asof_date="2026-08-27",
            calibration_reference_notional_usd=1_000_000.0,
        )
    with pytest.raises(ValueError, match="cannot predate promotion evidence"):
        validate_capital_context_binding(
            portfolio_capital_context=_portfolio_capital_context(
                asof_date="2026-08-26"
            ),
            capital_context_file_sha256=_digest("5"),
            trusted_capital_context_file_sha256=_digest("5"),
            evidence_asof_date="2026-08-27",
            calibration_reference_notional_usd=1_000_000.0,
        )


def test_decision_binding_translates_manifest_digest_to_v2_input_panel_field() -> None:
    bindings = {
        "candidate_registry_sha256": _digest("1"),
        "input_manifest_sha256": _digest("2"),
        "fold_registry_sha256": _digest("3"),
        "code_sha256": _digest("4"),
    }
    decision = {
        "candidate_registry_sha256": bindings["candidate_registry_sha256"],
        "input_panel_sha256": bindings["input_manifest_sha256"],
        "fold_registry_sha256": bindings["fold_registry_sha256"],
        "code_sha256": bindings["code_sha256"],
    }

    _validate_decision_bindings(decision, bindings)

    wrong_alias = dict(decision)
    wrong_alias.pop("input_panel_sha256")
    wrong_alias["input_manifest_sha256"] = bindings["input_manifest_sha256"]
    with pytest.raises(ValueError, match="decision.input_panel_sha256"):
        _validate_decision_bindings(wrong_alias, bindings)


def test_rehashed_attestation_cannot_claim_fresh_chronological_evidence() -> None:
    tampered = copy.deepcopy(_attestation())
    tampered["evidence_role"] = "fresh_chronological"
    tampered["payload_sha256"] = value_sha256(
        {key: value for key, value in tampered.items() if key != "payload_sha256"}
    )
    with pytest.raises(ValueError, match="policy changed"):
        validate_input_build_attestation(tampered)


def _outer_fixture() -> tuple[dict, dict, list[dict]]:
    cohort = "beverages"
    horizon = 63
    path = [{
        "observation_id": "daily-1",
        "source_portfolio_observation_id": "outer-1",
        "fold_id": "wf_001",
        "cohort": cohort,
        "horizon_sessions": horizon,
        "signal_date": "2026-01-02",
        "return_date": "2026-01-05",
        "gross_return": 0.011,
        "transaction_cost": 0.001,
        "net_return": 0.010,
    }]
    outer_v2 = [{
        "observation_id": "outer-1",
        "fold_id": "wf_001",
        "asof_date": "2026-01-02",
        "label_completion_date": "2026-04-02",
    }]
    realized_v2 = [{
        "observation_id": "daily-1",
        "source_portfolio_observation_id": "outer-1",
        "fold_id": "wf_001",
        "return_date": "2026-01-05",
        "net_strategy_return": 0.010,
    }]
    matrix_sha = _digest("2")
    result = {
        "performance": {"paired_observation_count": 1},
        "evidence": {
            "evaluation_role": "outer_test",
            "horizon_sessions": horizon,
            "observation_count": 1,
            "observation_ids_sha256": value_sha256({"value": outer_v2}),
            "fold_ids_sha256": value_sha256({"value": [{"fold_id": "wf_001", "test_dates": ["2026-01-02"]}]}),
            "signal_start_date": "2026-01-02",
            "signal_end_date": "2026-01-02",
            "latest_label_completion_date": "2026-04-02",
            "realized_return_count": 1,
            "realized_return_stream_sha256": value_sha256({"value": realized_v2}),
            "realized_return_start_date": "2026-01-05",
            "realized_return_end_date": "2026-01-05",
            "candidate_matrix_sha256": matrix_sha,
        },
    }
    detail = {
        "realized_path_attestation_sha256": value_sha256(path),
        "completion_by_signal_date": {"2026-01-02": "2026-04-02"},
        "folds": [{"fold_id": "wf_001", "test_dates": ["2026-01-02"]}],
        "selected_candidate_by_fold": {"wf_001": "candidate-1"},
        "candidate_matrix_sha256": matrix_sha,
        "outer_observation_count": 1,
        "realized_daily_return_count": 1,
    }
    return result, detail, path


def test_outer_oos_reconstruction_proves_ids_chronology_and_net_returns() -> None:
    result, detail, path = _outer_fixture()
    rows = _outer_oos_from_path(
        cohort="beverages",
        horizon=63,
        result=result,
        detail=detail,
        path_rows=path,
        asof_date="2026-08-27",
    )
    assert rows == [{
        "observation_id": "outer-1",
        "fold_id": "wf_001",
        "signal_date": "2026-01-02",
        "label_completion_date": "2026-04-02",
    }]


def test_outer_oos_reconstruction_rejects_non_net_returns() -> None:
    result, detail, path = _outer_fixture()
    path[0]["net_return"] = 0.02
    detail["realized_path_attestation_sha256"] = value_sha256(path)
    with pytest.raises(ValueError, match="not net of transaction cost"):
        _outer_oos_from_path(
            cohort="beverages",
            horizon=63,
            result=result,
            detail=detail,
            path_rows=path,
            asof_date="2026-08-27",
        )
