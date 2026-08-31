"""Canonical future evaluator: signed sources, exact timestamps, independent sleeves."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .independent_verdicts import add_independent_sleeve_actions
from .interval_integrity import validate_interval_timestamps
from .protocol import FutureEvidencePolicy, canonical_sha256
from .protocol_v3 import evaluate_future_evidence_v3
from .trusted_receipts import PinnedEd25519Authority


def evaluate_future_evidence_v4(
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
    timing = validate_interval_timestamps(
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        trading_calendar_path=trading_calendar_path,
    )
    result = evaluate_future_evidence_v3(
        policy=policy,
        family=family,
        benchmark_ticker=benchmark_ticker,
        authority=authority,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        outcome_source_paths=outcome_source_paths,
        outcome_receipt_path=outcome_receipt_path,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        trading_calendar_path=trading_calendar_path,
        evaluation_at_utc=evaluation_at_utc,
    )
    result.pop("payload_sha256", None)
    result.update(
        schema_version="future_only_evidence_evaluation_v4",
        interval_timing_integrity=timing,
    )
    result["payload_sha256"] = canonical_sha256(result)
    return add_independent_sleeve_actions(result)


__all__ = ["evaluate_future_evidence_v4"]
