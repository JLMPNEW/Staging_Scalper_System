from __future__ import annotations

import json
from pathlib import Path

from consumer_defensive.core.future_oos_preflight_v2 import build_operational_preflight


def test_untrusted_draft_is_named_explicitly(tmp_path: Path) -> None:
    plan = tmp_path / "draft.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": "consumer_defensive_monthly_target_plan_v1_draft",
                "status": "draft_requires_new_registry_and_source_seal",
                "registered_before_target_access": False,
            }
        ),
        encoding="utf-8",
    )
    result = build_operational_preflight(plan_path=plan, asof_date="2026-08-25")
    assert result["clock_started"] is False
    assert result["current_artifact_classification"] == "untrusted_draft_not_future_evidence"
    assert any("untrusted draft" in blocker for blocker in result["blockers"])
