from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from med_devices.core.config import cfg_get


PROMOTED_GATE_POLICY_PATH = "calibration.calibrated_baseline.promoted_gate_policies"
ALLOWED_GATE_KEYS = {
    "composite_min",
    "cohort_percentile_min",
    "fundamental_quality_min",
    "durable_growth_min",
    "fda_product_min",
    "reimbursement_min",
    "valuation_min",
    "technical_entry_min",
    "value_trap_max",
    "data_completeness_min",
}


def _iso_date(raw: object, *, context: str) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    value = str(raw or "").strip()[:10]
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO date (YYYY-MM-DD); got {raw!r}") from exc


def effective_promoted_gate_overrides(
    config: dict[str, Any],
    *,
    cohort: str,
    asof_raw: object,
) -> dict[str, float]:
    """Return the reviewed cohort gate policy effective at asof_raw.

    Policies are intentionally effective-dated so a production promotion cannot
    rewrite the decision rules used by earlier point-in-time snapshots.
    """
    raw_policies = cfg_get(config, PROMOTED_GATE_POLICY_PATH, {}) or {}
    if not isinstance(raw_policies, dict):
        raise ValueError(f"{PROMOTED_GATE_POLICY_PATH} must be a mapping")
    raw_policy = raw_policies.get(cohort)
    if raw_policy is None or raw_policy == "":
        return {}
    if not isinstance(raw_policy, dict):
        raise ValueError(f"{PROMOTED_GATE_POLICY_PATH}.{cohort} must be a mapping")
    if not str(raw_policy.get("source_parameter_set_id") or "").strip():
        raise ValueError(f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.source_parameter_set_id is required")

    effective_from = _iso_date(
        raw_policy.get("effective_from"),
        context=f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.effective_from",
    )
    reviewed_at = _iso_date(
        raw_policy.get("reviewed_at"),
        context=f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.reviewed_at",
    )
    asof = _iso_date(asof_raw, context="score asof_date")
    if asof < max(effective_from, reviewed_at):
        return {}

    raw_gates = raw_policy.get("gates")
    if not isinstance(raw_gates, dict) or not raw_gates:
        raise ValueError(f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.gates must be a non-empty mapping")
    unknown = sorted(set(raw_gates) - ALLOWED_GATE_KEYS)
    if unknown:
        raise ValueError(f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.gates contains unsupported keys: {','.join(unknown)}")

    gates: dict[str, float] = {}
    for key, raw_value in raw_gates.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.gates.{key} must be numeric; got {raw_value!r}"
            ) from exc
        if not math.isfinite(value):
            raise ValueError(f"{PROMOTED_GATE_POLICY_PATH}.{cohort}.gates.{key} must be finite; got {raw_value!r}")
        gates[key] = value
    return gates
