from pathlib import Path

from consumer_defensive.core.future_oos_preflight_v3 import build_operational_preflight


def test_current_consumer_clock_is_explicitly_stopped(tmp_path: Path) -> None:
    plan = tmp_path / "draft.json"
    plan.write_text("{}", encoding="utf-8")
    payload = build_operational_preflight(plan_path=plan, asof_date="2026-08-25")
    assert payload["clock_started"] is False
    assert payload["status"] == "clock_not_started"
    assert payload["validated_prospective_capture_count"] == 0
    assert payload["remaining_nonoverlapping_observations"] == {"21": 12, "63": 6, "126": 4}
    assert payload["calendar_date_guarantee"] is False

