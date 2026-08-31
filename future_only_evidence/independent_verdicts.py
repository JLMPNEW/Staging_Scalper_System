"""Make cohort/sleeve promotion eligibility explicitly independent."""

from __future__ import annotations

from typing import Any, Mapping

from .protocol import canonical_sha256


def add_independent_sleeve_actions(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    sleeves = list(result.get("sleeve_verdicts") or [])
    passing = sorted(str(row["sleeve_id"]) for row in sleeves if row.get("pass") is True)
    failing = sorted(str(row["sleeve_id"]) for row in sleeves if row.get("pass") is not True)
    result.pop("payload_sha256", None)
    result.update(
        independent_sleeve_submission_actions=[
            {
                "sleeve_id": sleeve,
                "action": "submit_this_sleeve_for_independent_promotion_review",
                "production_activation_authorized": False,
            }
            for sleeve in passing
        ],
        passing_sleeves=passing,
        blocked_sleeves=failing,
        any_sleeve_pass=bool(passing),
        sector_wide_all_sleeves_pass=bool(sleeves) and not failing,
        sector_wide_action=(
            "all_sleeves_may_submit_independently" if sleeves and not failing
            else "do_not_use_sector_aggregate_to_block_or_rescue_individual_sleeves"
        ),
        action=(
            "submit_passing_sleeves_for_independent_review"
            if passing
            else "remain_shadow_fail_closed"
        ),
        production_activation_authorized=False,
        portfolio_write_enabled=False,
        optimizer_cap=0.0,
    )
    result["payload_sha256"] = canonical_sha256(result)
    return result


__all__ = ["add_independent_sleeve_actions"]
