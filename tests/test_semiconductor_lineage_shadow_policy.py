from __future__ import annotations

import runpy
from pathlib import Path

from orchestration_contracts.financial_lineage import (
    POLICY_CANDIDATE_ONLY,
    POLICY_DISABLED,
    policy_for_model_family,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_semiconductor_lineage_is_shadow_only_in_central_policy() -> None:
    policy = policy_for_model_family("semiconductors")

    assert policy.enabled is True
    assert policy.mode_for("production") == POLICY_DISABLED
    assert policy.mode_for("research") == POLICY_CANDIDATE_ONLY
    assert policy.mode_for("historical") == POLICY_DISABLED


def test_global_orchestrator_does_not_require_semiconductor_shadow_in_production() -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    registry = namespace["load_registry"](PROJECT_ROOT / "orchestration" / "registry.yaml")
    semiconductors = registry.by_name("semiconductors")

    assert semiconductors.financial_lineage_required is False
    assert semiconductors.financial_lineage_policy == POLICY_DISABLED


def test_local_semiconductor_shadow_runs_after_publish_and_is_nonblocking() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "technology" / "semiconductors" / "scripts" / "17_run_semiconductor_refresh_pipeline.py")
    )
    steps = namespace["build_steps"](
        asof="2026-08-14",
        manual_wsts_xlsx=None,
        skip_ibkr_borrow=False,
        force_refresh=False,
        financial_batch_size=8,
        financial_batch_timeout_sec=900.0,
    )
    step_ids = [step.step_id for step in steps]
    shadow = next(step for step in steps if step.step_id == "10c_financial_lineage_shadow")

    assert step_ids.index("10b_publish_dashboard") < step_ids.index("10c_financial_lineage_shadow")
    assert step_ids.index("10c_financial_lineage_shadow") < step_ids.index("10b_validate_dashboard")
    assert shadow.blocking is False
    assert shadow.args == ["--asof", "2026-08-14"]
