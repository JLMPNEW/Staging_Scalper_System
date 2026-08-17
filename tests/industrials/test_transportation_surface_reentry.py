from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "38_audit_transportation_surface_reentry.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "transportation_surface_reentry_test_module", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reentry_decision_passes_only_when_every_gate_passes() -> None:
    module = load_module()
    status, blockers = module.decide_candidate(
        {
            "active_membership": True,
            "market": True,
            "financial_policy": True,
            "current_required_metrics": True,
            "required_financial_history": True,
            "solvency_evidence": True,
            "positioning": True,
            "annual_revenue_integrity": True,
        }
    )
    assert status == "PASS"
    assert blockers == ()


def test_reentry_decision_is_fail_closed_and_deterministic() -> None:
    module = load_module()
    status, blockers = module.decide_candidate(
        {
            "market": True,
            "solvency_evidence": False,
            "annual_revenue_integrity": False,
        }
    )
    assert status == "BLOCKED"
    assert blockers == ("annual_revenue_integrity", "solvency_evidence")


def test_explicit_zero_debt_interest_coverage_na_is_resolved() -> None:
    module = load_module()
    assert module._interest_coverage_resolved(
        {
            "availability_status": "NOT_APPLICABLE",
            "metric_value": None,
            "status_reason": "issuer_has_explicit_zero_debt_and_no_interest_expense",
        }
    )
    assert not module._interest_coverage_resolved(
        {
            "availability_status": "NOT_APPLICABLE",
            "metric_value": None,
            "status_reason": "issuer_did_not_report_metric",
        }
    )
