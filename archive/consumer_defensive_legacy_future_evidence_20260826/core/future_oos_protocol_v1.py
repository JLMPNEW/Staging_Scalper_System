"""Prospective-only Consumer Defensive holdout protocol.

This module does not retrofit preregistration onto the already-accessed
Stage 8/9 dates.  A valid clock starts only after a frozen baseline plan is
bound to an independently verified registration receipt.  Every later rank,
membership, source, and terminal-event snapshot is hash-bound at capture.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.protocol import (
    FutureEvidencePolicy,
    TrustedReceiptVerifier,
    build_capture_payload,
    canonical_sha256,
    evaluate_future_evidence,
    exact_sha256,
    file_sha256,
)


SCHEMA_VERSION = "consumer_defensive_future_oos_protocol_v1"
PLAN_SCHEMA = "consumer_defensive_future_oos_plan_v1"
REQUIRED_PLAN_ROLES = frozenset(
    {
        "candidate_registry",
        "universe_contract",
        "frozen_baseline_spec",
        "source_registry",
        "terminal_event_policy",
    }
)
REQUIRED_CAPTURE_ROLES = frozenset(
    {
        *REQUIRED_PLAN_ROLES,
        "rank_snapshot",
        "membership_snapshot",
        "source_manifest",
    }
)
DEFAULT_MINIMUM_COUNTS = {21: 12, 63: 6, 126: 4}


def _utc(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{label} must be timezone-aware UTC")
    return parsed


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} is missing: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return dict(payload)


def _plan_policy(plan: Mapping[str, Any]) -> FutureEvidencePolicy:
    thresholds = plan.get("acceptance_thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("plan acceptance_thresholds must be an object")
    counts = plan.get("minimum_nonoverlapping_outcomes")
    if not isinstance(counts, dict):
        raise ValueError("plan minimum_nonoverlapping_outcomes must be an object")
    normalized_counts = {int(key): int(value) for key, value in counts.items()}
    if normalized_counts != DEFAULT_MINIMUM_COUNTS:
        raise ValueError("Consumer future counts must remain exactly 12/6/4 for 21/63/126")
    minimum_cross_sections = plan.get("minimum_cross_sections")
    if not isinstance(minimum_cross_sections, dict) or not minimum_cross_sections:
        raise ValueError("plan must freeze cohort-specific minimum cross-sections")
    return FutureEvidencePolicy(
        family="consumer_defensive",
        policy_id=str(plan["policy_id"]),
        effective_from=date.fromisoformat(str(plan["effective_from"])),
        first_signal_date=date.fromisoformat(str(plan["first_signal_date"])),
        horizons=(21, 63, 126),
        minimum_counts=normalized_counts,
        minimum_ic=float(thresholds["minimum_ic"]),
        minimum_top_minus_cohort=float(thresholds["minimum_top_minus_benchmark_net"]),
        minimum_top_minus_bottom=float(thresholds["minimum_top_minus_bottom_net"]),
        minimum_hit_rate=float(thresholds["minimum_sign_hit_rate"]),
        transaction_cost_bps=float(thresholds["transaction_cost_bps"]),
        minimum_cross_sections={str(key): int(value) for key, value in minimum_cross_sections.items()},
        require_group_pass=False,
        top_minus_bottom_basis="net",
    )


def validate_registered_plan(
    plan_path: Path,
    *,
    source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_receipt_verifier: TrustedReceiptVerifier | None,
) -> tuple[dict[str, Any], FutureEvidencePolicy]:
    """Validate the frozen baseline and independent pre-target anchor."""

    plan_resolved = plan_path.expanduser().resolve()
    plan = _read_json(plan_resolved, label="Consumer future plan")
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError("Consumer plan is a draft or unsupported schema")
    required_claims = {
        "evidence_class": "prospective_future_only",
        "baseline_state": "frozen_no_reestimation",
        "holdout_role": "prospective_holdout_only",
        "status": "registered_trusted",
    }
    for field, expected in required_claims.items():
        if plan.get(field) != expected:
            raise ValueError(f"Consumer plan {field} must be {expected!r}")
    if plan.get("legacy_revealed_dates_can_authorize") is not False:
        raise ValueError("revealed Stage 8/9 dates cannot authorize future promotion")
    if plan.get("historical_results_can_authorize_production") is not False:
        raise ValueError("historical diagnostics cannot authorize future promotion")
    if set(source_paths) != REQUIRED_PLAN_ROLES:
        raise ValueError("Consumer plan source roles do not exactly match the contract")
    expected_hashes = plan.get("registered_source_sha256")
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != REQUIRED_PLAN_ROLES:
        raise ValueError("Consumer plan must bind every registered source hash")
    for role in sorted(REQUIRED_PLAN_ROLES):
        actual = file_sha256(source_paths[role].expanduser().resolve())
        if actual != exact_sha256(expected_hashes[role], label=f"{role} sha256"):
            raise ValueError(f"registered Consumer source changed: {role}")
    if registration_receipt_verifier is None:
        raise ValueError("Consumer registration requires an independent trusted receipt verifier")
    receipt_resolved = registration_receipt_path.expanduser().resolve()
    actual_receipt_hash = file_sha256(receipt_resolved)
    if actual_receipt_hash != exact_sha256(
        expected_registration_receipt_sha256,
        label="registration receipt sha256",
    ):
        raise ValueError("Consumer registration receipt hash mismatch")
    receipt = _read_json(receipt_resolved, label="Consumer registration receipt")
    if registration_receipt_verifier(receipt_resolved, actual_receipt_hash, receipt) is not True:
        raise ValueError("Consumer registration receipt failed independent verification")
    if receipt.get("schema_version") != "consumer_defensive_registration_receipt_v1":
        raise ValueError("unsupported Consumer registration receipt")
    plan_hash = file_sha256(plan_resolved)
    if receipt.get("plan_sha256") != plan_hash:
        raise ValueError("registration receipt does not bind exact plan bytes")
    if receipt.get("registered_source_sha256") != expected_hashes:
        raise ValueError("registration receipt does not bind exact registered sources")
    registered_at = _utc(receipt.get("registered_at_utc"), label="registered_at_utc")
    first_signal = date.fromisoformat(str(plan["first_signal_date"]))
    if registered_at.date() >= first_signal:
        raise ValueError("Consumer registration was not anchored before the first signal date")
    return plan, _plan_policy(plan)


def build_preflight(
    *,
    plan_path: Path,
    asof_date: str,
    source_paths: Mapping[str, Path] | None = None,
    registration_receipt_path: Path | None = None,
    expected_registration_receipt_sha256: str = "",
    registration_receipt_verifier: TrustedReceiptVerifier | None = None,
) -> dict[str, Any]:
    """Return a safe readiness artifact; all failures leave a zero-cap state."""

    blockers: list[str] = []
    plan: dict[str, Any] = {}
    policy: FutureEvidencePolicy | None = None
    try:
        if source_paths is None or registration_receipt_path is None:
            raise ValueError("trusted registered plan inputs are not configured")
        plan, policy = validate_registered_plan(
            plan_path,
            source_paths=source_paths,
            registration_receipt_path=registration_receipt_path,
            expected_registration_receipt_sha256=expected_registration_receipt_sha256,
            registration_receipt_verifier=registration_receipt_verifier,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        blockers.append(str(exc))
    asof = date.fromisoformat(str(asof_date)[:10])
    clock_started = policy is not None and asof >= policy.first_signal_date
    if policy is not None and not clock_started:
        blockers.append("first prospective signal date has not arrived")
    status = "ready_for_signal_capture" if clock_started and not blockers else "clock_not_started"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "consumer_defensive_future_oos_preflight",
        "asof_date": asof.isoformat(),
        "status": status,
        "clock_started": clock_started,
        "trusted_registration_pass": policy is not None,
        "plan_schema": plan.get("schema_version", ""),
        "blockers": blockers,
        "minimum_nonoverlapping_outcomes": DEFAULT_MINIMUM_COUNTS,
        "legacy_revealed_dates_counted": 0,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "next_data_needed": [
            "final frozen baseline plan with exact candidate/universe/source hashes",
            "independently verifiable pre-target registration receipt",
            "first fresh post-registration monthly rank and membership snapshot",
            "later exact-census 21/63/126-session outcomes with terminal-event dispositions",
        ],
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def capture_signal(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_receipt_verifier: TrustedReceiptVerifier | None,
    asof_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    trusted_capture_receipt_path: Path,
    expected_trusted_capture_receipt_sha256: str,
    trusted_capture_receipt_verifier: TrustedReceiptVerifier | None,
) -> dict[str, Any]:
    plan, policy = validate_registered_plan(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=registration_receipt_verifier,
    )
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES:
        raise ValueError("Consumer capture source roles do not exactly match the contract")
    for role in REQUIRED_PLAN_ROLES:
        if file_sha256(capture_source_paths[role]) != file_sha256(plan_source_paths[role]):
            raise ValueError(f"Consumer capture changed registered source: {role}")
    payload = build_capture_payload(
        policy=policy,
        asof_date=asof_date,
        capture_date=asof_date,
        signal_rows=signal_rows,
        source_paths=capture_source_paths,
        expected_source_sha256=expected_capture_source_sha256,
        required_source_roles=REQUIRED_CAPTURE_ROLES,
        trusted_receipt_path=trusted_capture_receipt_path,
        expected_trusted_receipt_sha256=expected_trusted_capture_receipt_sha256,
        trusted_receipt_verifier=trusted_capture_receipt_verifier,
    )
    payload["registration_plan_sha256"] = file_sha256(plan_path)
    payload["registration_receipt_sha256"] = file_sha256(registration_receipt_path)
    payload["baseline_state"] = plan["baseline_state"]
    payload["prospective_membership_tracking_required"] = True
    payload["terminal_event_tracking_required"] = True
    payload.pop("capture_id")
    payload.pop("payload_sha256")
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def evaluate(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_receipt_verifier: TrustedReceiptVerifier | None,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    _, policy = validate_registered_plan(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=registration_receipt_verifier,
    )
    return evaluate_future_evidence(
        policy=policy,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        evaluation_at_utc=evaluation_at_utc,
    )


__all__ = [
    "DEFAULT_MINIMUM_COUNTS",
    "PLAN_SCHEMA",
    "REQUIRED_CAPTURE_ROLES",
    "REQUIRED_PLAN_ROLES",
    "build_preflight",
    "capture_signal",
    "evaluate",
    "validate_registered_plan",
]
