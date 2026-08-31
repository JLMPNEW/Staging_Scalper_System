"""Canonical Consumer preflight: clock starts only with a trusted capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from future_only_evidence.canonical_trust import CANONICAL_TRUST_REGISTRY
from future_only_evidence.protocol import canonical_sha256, file_sha256

from .future_oos_preflight_v2 import build_operational_preflight as _v2


SESSION_LOWER_BOUNDS = {21: 252, 63: 378, 126: 504}
REMAINING_COUNTS = {21: 12, 63: 6, 126: 4}


def build_operational_preflight(*, plan_path: Path, asof_date: str, **kwargs: Any) -> dict[str, Any]:
    payload = _v2(plan_path=plan_path, asof_date=asof_date, **kwargs)
    trust = json.loads(CANONICAL_TRUST_REGISTRY.read_text(encoding="utf-8"))
    blockers = set(payload.get("blockers") or [])
    if trust.get("status") != "active_reviewed":
        blockers.add("canonical evidence/timestamp/market-data trust roots are unconfigured")
    blockers.update(
        {
            "no reviewed registered Consumer fixed-horizon plan exists",
            "no externally timestamped immutable prospective capture exists",
            "no signed append-only capture-registry head exists",
            "no independently attested raw XLP/stock open-execution outcome export exists",
        }
    )
    payload.pop("payload_sha256", None)
    payload.update(
        schema_version="consumer_defensive_future_oos_preflight_v3",
        status="clock_not_started",
        clock_started=False,
        ready_for_capture=False,
        validated_prospective_capture_count=0,
        canonical_trust_registry={
            "path": str(CANONICAL_TRUST_REGISTRY.resolve()),
            "sha256": file_sha256(CANONICAL_TRUST_REGISTRY),
            "status": trust.get("status"),
            "registry_revision": trust.get("registry_revision"),
        },
        current_artifact_classification="untrusted_or_revealed_diagnostic_rejected",
        current_diagnostic_artifacts_counted=0,
        remaining_nonoverlapping_observations={
            str(key): value for key, value in REMAINING_COUNTS.items()
        },
        earliest_session_lower_bound_from_first_valid_entry={
            str(key): value for key, value in SESSION_LOWER_BOUNDS.items()
        },
        session_math=(
            "12x21=252; 6x63=378; 4x126=504 market sessions. "
            "These are lower bounds, not calendar-date guarantees."
        ),
        calendar_date_guarantee=False,
        blockers=sorted(blockers),
        production_activation_authorized=False,
        portfolio_write_enabled=False,
        optimizer_cap=0.0,
    )
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["REMAINING_COUNTS", "SESSION_LOWER_BOUNDS", "build_operational_preflight"]
