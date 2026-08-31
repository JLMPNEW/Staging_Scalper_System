"""Truthful current-state classification for Consumer future preflight."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from future_only_evidence.protocol import canonical_sha256

from .future_oos_preflight_v1 import build_operational_preflight as _v1
from .future_oos_protocol_v1 import PLAN_SCHEMA


def build_operational_preflight(*, plan_path: Path, **kwargs: Any) -> dict[str, Any]:
    payload = _v1(plan_path=plan_path, **kwargs)
    plan: dict[str, Any] = {}
    try:
        loaded = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            plan = loaded
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    schema = str(plan.get("schema_version") or "")
    status = str(plan.get("status") or "")
    blockers = list(payload["blockers"])
    if schema != PLAN_SCHEMA:
        blockers.append("current Consumer plan is an untrusted draft, not a registered prospective plan")
    if bool(plan.get("registered_before_target_access")) is not True:
        blockers.append("current local registration claim is absent/untrusted")
    payload.pop("payload_sha256", None)
    payload.update(
        schema_version="consumer_defensive_future_oos_preflight_v2",
        plan_schema=schema,
        plan_status=status,
        current_artifact_classification="untrusted_draft_not_future_evidence",
        blockers=sorted(set(blockers)),
        status="clock_not_started",
        clock_started=False,
    )
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["build_operational_preflight"]
