from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

from orchestration_contracts.financial_lineage import (
    POLICY_CANDIDATE_ONLY,
    POLICY_DISABLED,
    policy_for_model_family,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "model_family",
    ["semiconductors", "software_infrastructure", "technology_hardware"],
)
def test_all_technology_families_have_activation_bounded_production_lineage(
    model_family: str,
) -> None:
    policy = policy_for_model_family(model_family)

    assert policy.enabled is True
    assert policy.mode_for("production") == POLICY_CANDIDATE_ONLY
    assert policy.production_valid_from == "2026-08-14"
    assert policy.mode_for_asof("production", "2026-08-13") == POLICY_DISABLED
    assert policy.mode_for_asof("production", "2026-08-14") == POLICY_CANDIDATE_ONLY
    assert policy.mode_for("research") == POLICY_CANDIDATE_ONLY
    assert policy.mode_for("historical") == POLICY_DISABLED
    assert policy.require_score_incorporation is True
    assert policy.require_live_source_discovery is True


@pytest.mark.parametrize(
    ("script_path", "model_family"),
    [
        (
            "technology/semiconductors/scripts/17_run_semiconductor_refresh_pipeline.py",
            "semiconductors",
        ),
        (
            "technology/software_infrastructure/scripts/17_run_software_infrastructure_refresh_pipeline.py",
            "software_infrastructure",
        ),
        (
            "technology/technology_hardware/scripts/17_run_technology_hardware_refresh_pipeline.py",
            "technology_hardware",
        ),
    ],
)
def test_local_technology_runners_recover_then_publish_then_shadow(
    script_path: str,
    model_family: str,
) -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / script_path))
    kwargs: dict[str, Any] = {
        "asof": "2026-08-14",
        "skip_ibkr_borrow": False,
        "force_refresh": False,
        "financial_batch_size": 8,
        "financial_batch_timeout_sec": 900.0,
    }
    if model_family == "semiconductors":
        kwargs["manual_wsts_xlsx"] = None
    steps = namespace["build_steps"](**kwargs)
    ids = [step.step_id for step in steps]
    sec_sync = next(step for step in steps if step.step_id == "07_sync_sec_fundamentals")
    financial_build = next(
        step for step in steps if step.step_id == "08_build_financial_features"
    )
    recovery = next(step for step in steps if step.step_id == "07b_recover_6k_financials")
    shadow = next(step for step in steps if step.step_id == "10c_financial_lineage_shadow")

    assert sec_sync.args[:3] == [
        "--asof",
        "2026-08-14",
        "--force-submissions-refresh",
    ]
    assert "--current-members-only" in sec_sync.args
    assert "--current-members-only" in financial_build.args
    assert ids.index("07_sync_sec_fundamentals") < ids.index("07b_recover_6k_financials")
    assert ids.index("07b_recover_6k_financials") < ids.index("08_build_financial_features")
    assert ids.index("10b_publish_dashboard") < ids.index("10c_financial_lineage_shadow")
    assert ids.index("10c_financial_lineage_shadow") < ids.index("10b_validate_dashboard")
    assert recovery.args[:2] == ["--family", model_family]
    assert shadow.args == [
        "--family",
        model_family,
        "--policy-context",
        "production",
        "--asof",
        "2026-08-14",
    ]
    assert shadow.blocking is True


def test_global_repair_rebuilds_lineage_shadow_for_all_technology_families() -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    registry = namespace["load_registry"](PROJECT_ROOT / "orchestration" / "registry.yaml")

    for model_family in (
        "semiconductors",
        "software_infrastructure",
        "technology_hardware",
    ):
        sector = registry.by_name(model_family)
        assert sector.financial_lineage_required is True
        assert sector.financial_lineage_policy == POLICY_CANDIDATE_ONLY
        assert sector.financial_lineage_artifact
        assert "{date}" in sector.financial_lineage_artifact
        assert sector.repair is not None
        steps = list(sector.repair.rebuild_steps)
        assert steps.index("10b_publish_dashboard") < steps.index(
            "10c_financial_lineage_shadow"
        )
        assert steps.index("10c_financial_lineage_shadow") < steps.index(
            "10b_validate_dashboard"
        )
