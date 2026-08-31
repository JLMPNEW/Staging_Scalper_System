"""Canonical Transportation preflight with missed-clock truthfulness."""

from __future__ import annotations

import json
from typing import Any

from future_only_evidence.canonical_trust import CANONICAL_TRUST_REGISTRY
from future_only_evidence.protocol import canonical_sha256, file_sha256

from .future_oos_preflight_v2 import build_operational_preflight as _v2


SESSION_LOWER_BOUNDS = {21: 252, 63: 252}
REMAINING_COUNTS_PER_SLEEVE = {21: 12, 63: 4}


def build_operational_preflight(**kwargs: Any) -> dict[str, Any]:
    payload = _v2(**kwargs)
    trust = json.loads(CANONICAL_TRUST_REGISTRY.read_text(encoding="utf-8"))
    blockers = set(payload.get("blockers") or [])
    if trust.get("status") != "active_reviewed":
        blockers.add("canonical evidence/timestamp/market-data trust roots are unconfigured")
    blockers.update(
        {
            "the 2026-08-24 eligible signal was not externally anchored before entry and is permanently ineligible",
            "a reviewed prospective-start amendment after trust-root activation is required; no backfill/backdating",
            "no externally timestamped immutable post-amendment v8 capture exists",
            "no signed append-only capture-registry head exists",
            "no independently attested raw IYT/stock open-execution outcome export exists",
        }
    )
    payload.pop("payload_sha256", None)
    payload.update(
        schema_version="transportation_v7_future_oos_preflight_v3",
        status="clock_not_started",
        clock_started=False,
        ready_for_capture=False,
        validated_prospective_capture_count=0,
        missed_original_first_signal_date="2026-08-24",
        missed_original_signal_can_be_backfilled=False,
        revised_future_start_required=True,
        canonical_trust_registry={
            "path": str(CANONICAL_TRUST_REGISTRY.resolve()),
            "sha256": file_sha256(CANONICAL_TRUST_REGISTRY),
            "status": trust.get("status"),
            "registry_revision": trust.get("registry_revision"),
        },
        current_artifact_classification="pre_effective_or_unanchored_diagnostic_rejected",
        current_diagnostic_artifacts_counted=0,
        remaining_nonoverlapping_observations_per_sleeve={
            str(key): value for key, value in REMAINING_COUNTS_PER_SLEEVE.items()
        },
        earliest_session_lower_bound_from_first_valid_entry={
            str(key): value for key, value in SESSION_LOWER_BOUNDS.items()
        },
        session_math=(
            "12x21=252 and 4x63=252 market sessions per sleeve. "
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


__all__ = [
    "REMAINING_COUNTS_PER_SLEEVE",
    "SESSION_LOWER_BOUNDS",
    "build_operational_preflight",
]
