"""Canonical promotion-evidence evaluator with lifecycle-safe outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .independent_verdicts import add_independent_sleeve_actions
from .interval_integrity import validate_interval_timestamps
from .outcome_integrity_v2 import validate_and_recompute_outcomes_v2
from .protocol import FutureEvidencePolicy, canonical_sha256
from .protocol_v2 import evaluate_future_evidence_v2
from .protocol_v3 import validate_capture_payload_v3
from .trusted_receipts import PinnedEd25519Authority


def evaluate_future_evidence_v5(
    *,
    policy: FutureEvidencePolicy,
    family: str,
    benchmark_ticker: str,
    authority: PinnedEd25519Authority,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    trading_calendar_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    for path in capture_paths:
        validate_capture_payload_v3(json.loads(Path(path).read_text(encoding="utf-8")))
    timing = validate_interval_timestamps(
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        trading_calendar_path=trading_calendar_path,
    )
    integrity = validate_and_recompute_outcomes_v2(
        family=family,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        outcome_source_paths=outcome_source_paths,
        outcome_receipt_path=outcome_receipt_path,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        authority=authority,
        benchmark_ticker=benchmark_ticker,
    )
    result = evaluate_future_evidence_v2(
        policy=policy,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        trading_calendar_path=trading_calendar_path,
        evaluation_at_utc=evaluation_at_utc,
    )
    for verdict in result["scope_verdicts"]:
        horizon = int(verdict["horizon_sessions"])
        for period in verdict["periods"]:
            key = f"{period['capture_id']}|{horizon}"
            period["benchmark_total_return"] = integrity["benchmark_by_period"][key]
    result.pop("payload_sha256", None)
    result.update(
        schema_version="future_only_evidence_evaluation_v5",
        interval_timing_integrity=timing,
        outcome_integrity=integrity,
    )
    result["payload_sha256"] = canonical_sha256(result)
    return add_independent_sleeve_actions(result)


__all__ = ["evaluate_future_evidence_v5"]
