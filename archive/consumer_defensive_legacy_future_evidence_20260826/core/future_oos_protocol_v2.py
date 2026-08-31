"""Calendar-bound evaluation for Consumer prospective-only evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.protocol import TrustedReceiptVerifier
from future_only_evidence.protocol_v2 import evaluate_future_evidence_v2

from .future_oos_protocol_v1 import validate_registered_plan


def evaluate(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_receipt_verifier: TrustedReceiptVerifier | None,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    trading_calendar_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    _, policy = validate_registered_plan(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=registration_receipt_verifier,
    )
    return evaluate_future_evidence_v2(
        policy=policy,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        trading_calendar_path=trading_calendar_path,
        evaluation_at_utc=evaluation_at_utc,
    )


__all__ = ["evaluate"]
