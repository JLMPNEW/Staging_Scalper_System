"""Explicit pre-effective/diagnostic classification for current v8 history."""

from __future__ import annotations

from datetime import date
from typing import Any

from future_only_evidence.protocol import canonical_sha256

from .future_oos_preflight_v1 import build_operational_preflight as _v1
from .future_oos_protocol_v1 import POLICY_EFFECTIVE_FROM


def build_operational_preflight(**kwargs: Any) -> dict[str, Any]:
    payload = _v1(**kwargs)
    latest = str(payload.get("latest_score_asof") or "")
    policy_effective_pass = bool(latest) and date.fromisoformat(latest) >= POLICY_EFFECTIVE_FROM
    blockers = list(payload["blockers"])
    if not policy_effective_pass:
        blockers.append("latest canonical v8 rows are pre-policy-effective and diagnostic-only")
    payload.pop("payload_sha256", None)
    payload.update(
        schema_version="transportation_v7_future_oos_preflight_v2",
        latest_score_policy_effective_pass=policy_effective_pass,
        current_artifact_classification=(
            "fresh_forward_shadow" if policy_effective_pass else "pre_effective_historical_diagnostic"
        ),
        blockers=sorted(set(blockers)),
        status="clock_not_started" if not policy_effective_pass else payload["status"],
        clock_started=bool(payload["clock_started"] and policy_effective_pass),
    )
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["build_operational_preflight"]
