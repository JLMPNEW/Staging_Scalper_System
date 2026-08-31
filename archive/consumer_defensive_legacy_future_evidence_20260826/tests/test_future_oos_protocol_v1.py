from __future__ import annotations

import json
from pathlib import Path

import pytest

from consumer_defensive.core.future_oos_preflight_v1 import build_operational_preflight
from consumer_defensive.core.future_oos_protocol_v1 import (
    DEFAULT_MINIMUM_COUNTS,
    REQUIRED_PLAN_ROLES,
    validate_registered_plan,
)
from future_only_evidence.protocol import file_sha256


def _write(path: Path, value: object) -> Path:
    path.write_text(
        value if isinstance(value, str) else json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_current_draft_does_not_retrofit_preregistration_and_is_zero_cap(tmp_path: Path) -> None:
    draft = _write(
        tmp_path / "draft.json",
        {
            "schema_version": "consumer_defensive_monthly_target_plan_v1_draft",
            "status": "draft_requires_new_registry_and_source_seal",
        },
    )
    result = build_operational_preflight(plan_path=draft, asof_date="2026-08-25")
    assert result["status"] == "clock_not_started"
    assert result["clock_started"] is False
    assert result["valid_future_capture_count"] == 0
    assert result["remaining_outcome_count"] == DEFAULT_MINIMUM_COUNTS
    assert result["legacy_revealed_dates_counted"] == 0
    assert result["production_activation_authorized"] is False
    assert result["portfolio_write_enabled"] is False
    assert result["optimizer_cap"] == 0.0
    assert result["earliest_session_math"]["calendar_date_guarantee"] is False


def test_registered_plan_rejects_tampered_candidate_hash(tmp_path: Path) -> None:
    sources = {
        role: _write(tmp_path / f"{role}.txt", role)
        for role in REQUIRED_PLAN_ROLES
    }
    hashes = {role: file_sha256(path) for role, path in sources.items()}
    plan = _write(
        tmp_path / "plan.json",
        {
            "schema_version": "consumer_defensive_future_oos_plan_v1",
            "evidence_class": "prospective_future_only",
            "baseline_state": "frozen_no_reestimation",
            "holdout_role": "prospective_holdout_only",
            "status": "registered_trusted",
            "legacy_revealed_dates_can_authorize": False,
            "historical_results_can_authorize_production": False,
            "policy_id": "consumer_test_policy",
            "effective_from": "2026-08-26",
            "first_signal_date": "2026-08-27",
            "minimum_nonoverlapping_outcomes": {"21": 12, "63": 6, "126": 4},
            "acceptance_thresholds": {
                "minimum_ic": 0.0,
                "minimum_top_minus_benchmark_net": 0.0,
                "minimum_top_minus_bottom_net": 0.0,
                "minimum_sign_hit_rate": 0.55,
                "transaction_cost_bps": 20.0,
            },
            "minimum_cross_sections": {"beverages": 12},
            "registered_source_sha256": hashes,
        },
    )
    receipt = _write(
        tmp_path / "registration.json",
        {
            "schema_version": "consumer_defensive_registration_receipt_v1",
            "plan_sha256": file_sha256(plan),
            "registered_source_sha256": hashes,
            "registered_at_utc": "2026-08-26T12:00:00+00:00",
        },
    )
    validate_registered_plan(
        plan,
        source_paths=sources,
        registration_receipt_path=receipt,
        expected_registration_receipt_sha256=file_sha256(receipt),
        registration_receipt_verifier=lambda *_: True,
    )
    sources["candidate_registry"].write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_registry"):
        validate_registered_plan(
            plan,
            source_paths=sources,
            registration_receipt_path=receipt,
            expected_registration_receipt_sha256=file_sha256(receipt),
            registration_receipt_verifier=lambda *_: True,
        )


def test_wrong_future_counts_are_rejected(tmp_path: Path) -> None:
    sources = {
        role: _write(tmp_path / f"{role}.txt", role)
        for role in REQUIRED_PLAN_ROLES
    }
    hashes = {role: file_sha256(path) for role, path in sources.items()}
    plan = _write(
        tmp_path / "plan.json",
        {
            "schema_version": "consumer_defensive_future_oos_plan_v1",
            "evidence_class": "prospective_future_only",
            "baseline_state": "frozen_no_reestimation",
            "holdout_role": "prospective_holdout_only",
            "status": "registered_trusted",
            "legacy_revealed_dates_can_authorize": False,
            "historical_results_can_authorize_production": False,
            "policy_id": "consumer_test_policy",
            "effective_from": "2026-08-26",
            "first_signal_date": "2026-08-27",
            "minimum_nonoverlapping_outcomes": {"21": 11, "63": 6, "126": 4},
            "acceptance_thresholds": {
                "minimum_ic": 0.0,
                "minimum_top_minus_benchmark_net": 0.0,
                "minimum_top_minus_bottom_net": 0.0,
                "minimum_sign_hit_rate": 0.55,
                "transaction_cost_bps": 20.0,
            },
            "minimum_cross_sections": {"beverages": 12},
            "registered_source_sha256": hashes,
        },
    )
    receipt = _write(
        tmp_path / "registration.json",
        {
            "schema_version": "consumer_defensive_registration_receipt_v1",
            "plan_sha256": file_sha256(plan),
            "registered_source_sha256": hashes,
            "registered_at_utc": "2026-08-26T12:00:00+00:00",
        },
    )
    with pytest.raises(ValueError, match="12/6/4"):
        validate_registered_plan(
            plan,
            source_paths=sources,
            registration_receipt_path=receipt,
            expected_registration_receipt_sha256=file_sha256(receipt),
            registration_receipt_verifier=lambda *_: True,
        )
