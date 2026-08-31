"""Calendar-bound Transportation evaluation with explicit N/A groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.protocol_v2 import evaluate_future_evidence_v2

from .future_oos_protocol_v1 import TRANSPORT_POLICY


def evaluate(
    *,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    trading_calendar_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    result = evaluate_future_evidence_v2(
        policy=TRANSPORT_POLICY,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        trading_calendar_path=trading_calendar_path,
        evaluation_at_utc=evaluation_at_utc,
    )
    equal_weight: dict[tuple[str, str], set[str]] = {}
    for path in capture_paths:
        capture = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in capture.get("signal_rows", []):
            if row.get("ranking_mode") == "eligibility_equal_weight":
                key = (str(row["sleeve_id"]), str(row["group_id"]))
                equal_weight.setdefault(key, set()).add(str(capture["capture_id"]))
    not_applicable = [
        {
            "scope_kind": "group",
            "scope_id": group,
            "sleeve_id": sleeve,
            "horizon_sessions": horizon,
            "applicability": "not_applicable",
            "reason": "eligibility_equal_weight_has_no_rank_spread",
            "capture_count": len(capture_ids),
            "pass": None,
            "action": "monitor_eligibility_and_costs_not_predictive_gate",
        }
        for (sleeve, group), capture_ids in sorted(equal_weight.items())
        for horizon in TRANSPORT_POLICY.horizons
    ]
    result["scope_verdicts"].extend(not_applicable)
    applicable = [
        row
        for row in result["scope_verdicts"]
        if row.get("scope_kind") == "group" and row.get("applicability") != "not_applicable"
    ]
    result["group_scope_audit"] = {
        "applicable_predictive_verdict_count": len(applicable),
        "not_applicable_equal_weight_verdict_count": len(not_applicable),
        "not_applicable_excluded_from_group_pass_denominator": True,
    }
    result.pop("payload_sha256", None)
    result["schema_version"] = "transportation_future_only_evidence_evaluation_v3"
    result["payload_sha256"] = canonical_sha256(result)
    return result


__all__ = ["evaluate"]
