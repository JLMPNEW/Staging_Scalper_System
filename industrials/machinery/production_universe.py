from __future__ import annotations

from typing import Any, Mapping

from industrials.core.config import cfg_get


OPERATING_ONLY_UNIVERSE_POLICY = "operating_only"
ALL_RANK_READY_UNIVERSE_POLICY = "all_rank_ready"
SUPPORTED_UNIVERSE_POLICIES = frozenset(
    {
        OPERATING_ONLY_UNIVERSE_POLICY,
        ALL_RANK_READY_UNIVERSE_POLICY,
    }
)


def configured_universe_policy(
    config: Mapping[str, Any],
    *,
    config_key: str,
) -> str:
    policy = str(
        cfg_get(
            dict(config),
            f"{config_key}.production_universe_policy",
            OPERATING_ONLY_UNIVERSE_POLICY,
        )
    ).strip()
    if policy not in SUPPORTED_UNIVERSE_POLICIES:
        raise ValueError(
            f"Unsupported production universe policy: {policy!r}"
        )
    return policy


def production_universe_eligible(
    row: Mapping[str, Any],
    *,
    policy: str,
) -> bool:
    if policy == ALL_RANK_READY_UNIVERSE_POLICY:
        return True
    if policy != OPERATING_ONLY_UNIVERSE_POLICY:
        raise ValueError(
            f"Unsupported production universe policy: {policy!r}"
        )
    stage = str(row.get("development_stage") or "").strip().lower()
    cohort = str(row.get("calibration_cohort") or "").strip()
    return (
        stage not in {"development", "development_stage"}
        and cohort != "development_stage_emerging_machinery"
    )
