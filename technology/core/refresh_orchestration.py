from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class StepWithId(Protocol):
    @property
    def step_id(self) -> str: ...


GOVERNANCE_STEP_IDS = frozenset({"16_publish_governance", "16_validate_governance"})


def asof_governance_conflict(
    asof: str,
    steps: Iterable[StepWithId],
    *,
    publisher_script: str,
) -> str:
    """Return a fail-closed message when an as-of run includes latest-only governance."""
    asof = str(asof or "").strip()
    if not asof:
        return ""
    blocked = [step.step_id for step in steps if step.step_id in GOVERNANCE_STEP_IDS]
    if not blocked:
        return ""
    return (
        f"--asof {asof} cannot be combined with governance steps {blocked}: "
        f"{publisher_script} does not accept --asof and would record or validate "
        "latest-run artifacts under a historical context. Skip governance steps, or use "
        "technology/scripts/18_backfill_technology_historical_dashboard_reports.py for "
        "historical snapshots."
    )
