"""Signed-source, arithmetic-recomputed future evidence evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .outcome_integrity import validate_and_recompute_outcomes
from .protocol import (
    FutureEvidencePolicy,
    canonical_sha256,
    exact_sha256,
    validate_capture_payload,
)
from .protocol_v2 import evaluate_future_evidence_v2
from .trusted_receipts import PinnedEd25519Authority


def validate_capture_payload_v3(payload: Mapping[str, Any]) -> dict[str, Any]:
    capture = validate_capture_payload(payload)
    for row in capture["signal_rows"]:
        stored = exact_sha256(row.get("signal_row_sha256"), label="signal_row_sha256")
        unhashed = dict(row)
        unhashed.pop("signal_row_sha256", None)
        if canonical_sha256(unhashed) != stored:
            raise ValueError("stored signal-row SHA-256 does not bind exact row fields")
    return capture


def evaluate_future_evidence_v3(
    *,
    policy: FutureEvidencePolicy,
    family: str,
    benchmark_ticker: str | None,
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
    integrity = validate_and_recompute_outcomes(
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
            period["benchmark_total_return"] = integrity["benchmark_by_period"].get(key, 0.0)
    result.pop("payload_sha256", None)
    result.update(
        schema_version="future_only_evidence_evaluation_v3",
        outcome_integrity=integrity,
    )
    result["payload_sha256"] = canonical_sha256(result)
    return result


__all__ = ["evaluate_future_evidence_v3", "validate_capture_payload_v3"]
