"""Canonical Consumer signal capture with exact registered-census reconciliation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.canonical_archive import archive_file_once, archive_source_set
from future_only_evidence.canonical_domain import (
    attach_capture_timestamp,
    revalidate_capture_timestamp,
)
from future_only_evidence.canonical_trust import CanonicalTrustBundle
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.lifecycle_snapshot import (
    validate_lifecycle_capture_chronology,
    validate_lifecycle_event_snapshot,
)
from future_only_evidence.prospective_contracts import (
    PROSPECTIVE_ROLE,
    build_strict_capture,
    read_calendar_bytes,
    read_json_snapshot,
    read_source_snapshots,
    scheduled_asofs,
    validate_strict_capture,
)
from future_only_evidence.score_input_availability import (
    validate_score_input_availability_capture_chronology,
)

from .future_oos_capture_v4 import (
    REQUIRED_CAPTURE_ROLES_V2,
    SOURCE_GENERATION_STATE,
    SOURCE_MANIFEST_SCHEMA,
    derive_rank_signals,
)
from .future_oos_capture_v2 import REQUIRED_FRESHNESS_GATES
from .future_oos_plan_v5 import (
    COHORT_MINIMUMS,
    LIFECYCLE_EVENT_SCHEMA_V5,
    POLICY_ID,
    REQUIRED_PLAN_ROLES,
    validate_registered_plan_v5,
)
from .future_oos_score_lineage_v2 import validate_and_replay_consumer_scores


CAPTURE_DOMAIN_SCHEMA = "consumer_defensive_future_only_signal_capture_v5"
MEMBERSHIP_SCHEMA_V5 = "consumer_defensive_future_membership_snapshot_v2"
REQUIRED_CAPTURE_ROLES_V5 = frozenset(
    {
        *REQUIRED_CAPTURE_ROLES_V2,
        "atomic_feature_snapshot",
        "score_input_availability_snapshot",
        "score_input_availability_attestation",
        "lifecycle_event_snapshot",
        "lifecycle_source_attestation",
    }
)
MEMBERSHIP_TOP_LEVEL_FIELDS_V5 = frozenset(
    {"schema_version", "evidence_role", "asof_date", "rows", "rows_sha256"}
)
MEMBERSHIP_ROW_FIELDS_V5 = frozenset(
    {
        "asof_date",
        "ticker",
        "cohort_id",
        "group_id",
        "lifecycle_status_at_signal_cutoff",
        "lifecycle_eligible_flag",
        "model_data_eligible_flag",
        "model_data_exclusion_reason_codes",
        "final_signal_eligible_flag",
        "final_signal_exclusion_reason_codes",
    }
)
LIFECYCLE_POLICY = {
    "active": (1, None),
    "governed_terminal_event": (0, "lifecycle_governed_terminal_event"),
}


def _json(
    path: Path,
    *,
    label: str,
    snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    payload_bytes = (
        bytes(snapshot_bytes)
        if snapshot_bytes is not None
        else Path(path).expanduser().resolve().read_bytes()
    )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _exact_date(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD string")
    text = value
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return text


def _strict_flag(value: Any, *, label: str) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{label} must be a canonical integer 0/1")
    return value


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _snapshot_capture_sources(
    capture: Mapping[str, Any],
) -> tuple[dict[str, bytes], bytes]:
    identities = capture.get("source_identities")
    receipt = capture.get("trusted_receipt")
    if not isinstance(identities, dict) or not isinstance(receipt, dict):
        raise ValueError("previous Consumer capture lacks source/receipt identities")
    paths: dict[str, Path] = {}
    for role, identity in identities.items():
        if not isinstance(identity, dict):
            raise ValueError("previous Consumer capture source identity is invalid")
        paths[str(role)] = Path(str(identity.get("path") or ""))
    receipt_bytes = (
        Path(str(receipt.get("path") or "")).expanduser().resolve().read_bytes()
    )
    return read_source_snapshots(paths), receipt_bytes


def validate_capture_sources_v5(
    *,
    asof_date: str,
    source_paths: Mapping[str, Path],
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    asof = _exact_date(asof_date, label="Consumer capture asof")
    if set(source_paths) != REQUIRED_CAPTURE_ROLES_V5:
        raise ValueError("Consumer capture source role census changed")
    snapshots = (
        {role: bytes(value) for role, value in source_snapshot_bytes.items()}
        if source_snapshot_bytes is not None
        else read_source_snapshots(source_paths)
    )
    if set(snapshots) != REQUIRED_CAPTURE_ROLES_V5:
        raise ValueError("Consumer source snapshots changed the exact role census")
    manifest = _json(
        source_paths["source_manifest"],
        label="Consumer prospective source manifest",
        snapshot_bytes=snapshots["source_manifest"],
    )
    if (
        manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA
        or manifest.get("evidence_role") != PROSPECTIVE_ROLE
        or manifest.get("source_generation_state") != SOURCE_GENERATION_STATE
        or str(manifest.get("asof_date") or "") != asof
    ):
        raise ValueError("Consumer source manifest changed prospective identity/state")
    for field in (
        "historical_results_can_authorize_production",
        "production_activation_authorized",
    ):
        if manifest.get(field) is not False:
            raise ValueError(f"Consumer source manifest fail-closed field changed: {field}")
    freshness = manifest.get("freshness_gates")
    if (
        not isinstance(freshness, dict)
        or set(freshness) != REQUIRED_FRESHNESS_GATES
        or any(freshness[field] is not True for field in REQUIRED_FRESHNESS_GATES)
    ):
        raise ValueError("Consumer source freshness gate census failed")
    bound_roles = REQUIRED_CAPTURE_ROLES_V5 - {"source_manifest"}
    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict) or set(hashes) != bound_roles:
        raise ValueError("Consumer source manifest artifact roles are incomplete")
    observed_hashes = {
        role: hashlib.sha256(snapshots[role]).hexdigest()
        for role in sorted(REQUIRED_CAPTURE_ROLES_V5)
    }
    for role in bound_roles:
        if hashes[role] != observed_hashes[role]:
            raise ValueError(f"Consumer source manifest hash mismatch: {role}")
    return {
        "source_manifest_sha256": observed_hashes["source_manifest"],
        "rank_snapshot_sha256": observed_hashes["rank_snapshot"],
        "atomic_feature_snapshot_sha256": observed_hashes[
            "atomic_feature_snapshot"
        ],
        "membership_snapshot_sha256": observed_hashes["membership_snapshot"],
        "full_candidate_membership_census_required": True,
        "exact_prospective_role_pass": True,
        "freshness_gates_pass": True,
        "single_snapshot_semantic_validation_pass": True,
    }


def reconcile_exact_registered_census(
    *,
    asof_date: str,
    signals: list[dict[str, Any]],
    membership_path: Path,
    plan_audit: Mapping[str, Any],
    score_replay_audit: Mapping[str, Any],
    lifecycle_snapshot_path: Path,
    lifecycle_attestation_path: Path,
    expected_lifecycle_attestation_sha256: str,
    trust_bundle: CanonicalTrustBundle,
    signal_cutoff_at_utc: str,
    membership_snapshot_bytes: bytes | None = None,
    lifecycle_snapshot_bytes: bytes | None = None,
    lifecycle_attestation_bytes: bytes | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    asof = _exact_date(asof_date, label="Consumer membership asof")
    candidate = plan_audit["candidate_census"]
    candidate_tickers = set(candidate["candidate_tickers"])
    signal_index = {str(row["ticker"]): dict(row) for row in signals}
    if set(signal_index) != candidate_tickers or len(signal_index) != len(signals):
        raise ValueError(
            "Consumer rank snapshot is not the exact registered candidate census"
        )
    membership = _json(
        membership_path,
        label="Consumer membership snapshot",
        snapshot_bytes=membership_snapshot_bytes,
    )
    if set(membership) != MEMBERSHIP_TOP_LEVEL_FIELDS_V5:
        raise ValueError("Consumer membership top-level field census changed")
    if (
        membership.get("schema_version") != MEMBERSHIP_SCHEMA_V5
        or membership.get("evidence_role") != PROSPECTIVE_ROLE
        or membership.get("asof_date") != asof
    ):
        raise ValueError("Consumer membership snapshot identity/asof changed")
    rows = membership.get("rows")
    if (
        not isinstance(rows, list)
        or membership.get("rows_sha256") != canonical_sha256(rows)
    ):
        raise ValueError("Consumer membership rows are absent or hash-inconsistent")
    member_index: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != MEMBERSHIP_ROW_FIELDS_V5:
            raise ValueError("Consumer membership row field census changed")
        row = dict(raw)
        if row["asof_date"] != asof:
            raise ValueError("Consumer membership row asof differs from capture")
        ticker = row["ticker"]
        if (
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            or ticker in member_index
        ):
            raise ValueError("Consumer membership ticker census is invalid")
        member_index[ticker] = row
    if set(member_index) != candidate_tickers:
        raise ValueError("Consumer membership is not the exact registered candidate census")
    replay_index = score_replay_audit.get("model_data_eligibility_by_ticker")
    if not isinstance(replay_index, dict) or set(replay_index) != candidate_tickers:
        raise ValueError("Consumer replay eligibility census differs from candidates")
    lifecycle_index, lifecycle_audit = validate_lifecycle_event_snapshot(
        lifecycle_snapshot_path,
        expected_schema_version=LIFECYCLE_EVENT_SCHEMA_V5,
        asof_date=asof,
        expected_tickers=candidate_tickers,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        family="consumer_defensive",
        policy_id=str(plan_audit["domain_contract"]["policy_id"]),
        attestation_path=lifecycle_attestation_path,
        expected_attestation_sha256=expected_lifecycle_attestation_sha256,
        bundle=trust_bundle,
        snapshot_bytes=lifecycle_snapshot_bytes,
        attestation_bytes=lifecycle_attestation_bytes,
    )
    expected_by_cohort = {
        cohort: set(tickers)
        for cohort, tickers in candidate["cohort_tickers"].items()
    }
    eligible_by_cohort = {cohort: 0 for cohort in expected_by_cohort}
    resolution: list[dict[str, Any]] = []
    resolved_signals: list[dict[str, Any]] = []
    for ticker in sorted(signal_index):
        signal = signal_index[ticker]
        expected_cohort = next(
            cohort for cohort, tickers in expected_by_cohort.items() if ticker in tickers
        )
        if signal["sleeve_id"] != expected_cohort or signal["group_id"] != expected_cohort:
            raise ValueError(f"{ticker}: Consumer rank changed frozen cohort identity")
        member = member_index[ticker]
        if (
            str(member["cohort_id"]) != expected_cohort
            or str(member["group_id"]) != expected_cohort
        ):
            raise ValueError(f"{ticker}: Consumer membership changed frozen cohort identity")
        lifecycle_source = lifecycle_index[ticker]
        lifecycle_status = str(lifecycle_source["lifecycle_status_at_signal_cutoff"])
        if member["lifecycle_status_at_signal_cutoff"] != lifecycle_status:
            raise ValueError(
                f"{ticker}: membership lifecycle differs from independent source"
            )
        if lifecycle_status not in LIFECYCLE_POLICY:
            raise ValueError(f"{ticker}: lifecycle status is outside the frozen policy")
        expected_lifecycle, lifecycle_reason = LIFECYCLE_POLICY[lifecycle_status]
        lifecycle_flag = _strict_flag(
            member["lifecycle_eligible_flag"],
            label=f"{ticker} lifecycle eligibility",
        )
        if lifecycle_flag != expected_lifecycle:
            raise ValueError(f"{ticker}: lifecycle eligibility differs from frozen policy")
        replay = replay_index[ticker]
        if not isinstance(replay, dict):
            raise ValueError(f"{ticker}: replay eligibility row is invalid")
        model_flag = _strict_flag(
            replay.get("model_data_eligible_flag"),
            label=f"{ticker} replay model/data eligibility",
        )
        model_reasons = replay.get("model_data_exclusion_reason_codes")
        if (
            not isinstance(model_reasons, list)
            or any(not isinstance(value, str) or not value for value in model_reasons)
            or model_reasons != sorted(set(model_reasons))
            or (model_flag == 1 and model_reasons)
            or (model_flag == 0 and not model_reasons)
        ):
            raise ValueError(f"{ticker}: replay model/data reason census is invalid")
        if (
            _strict_flag(
                member["model_data_eligible_flag"],
                label=f"{ticker} membership model/data eligibility",
            )
            != model_flag
            or member["model_data_exclusion_reason_codes"] != model_reasons
        ):
            raise ValueError(f"{ticker}: membership differs from deterministic score replay")
        final_flag = lifecycle_flag & model_flag
        expected_reasons = sorted(
            set(([lifecycle_reason] if lifecycle_reason else []) + list(model_reasons))
        )
        if (
            _strict_flag(
                member["final_signal_eligible_flag"],
                label=f"{ticker} final signal eligibility",
            )
            != final_flag
            or member["final_signal_exclusion_reason_codes"] != expected_reasons
            or (final_flag == 1 and expected_reasons)
            or (final_flag == 0 and not expected_reasons)
        ):
            raise ValueError(
                f"{ticker}: final eligibility is not lifecycle intersect model/data"
            )
        if (
            _strict_flag(signal["eligible_flag"], label=f"{ticker} signal eligibility")
            != model_flag
            or _strict_flag(
                signal.get("predictive_eligible_flag"),
                label=f"{ticker} predictive eligibility",
            )
            != model_flag
        ):
            raise ValueError(f"{ticker}: rank eligibility differs from deterministic replay")
        resolved_signals.append(
            {
                **signal,
                "eligible_flag": final_flag,
                "predictive_eligible_flag": final_flag,
                "lifecycle_eligible_flag": lifecycle_flag,
                "model_data_eligible_flag": model_flag,
                "final_signal_exclusion_reason_codes": expected_reasons,
            }
        )
        eligible_by_cohort[expected_cohort] += final_flag
        resolution.append(
            {
                "ticker": ticker,
                "cohort_id": expected_cohort,
                "lifecycle_status_at_signal_cutoff": lifecycle_status,
                "terminal_event_type": lifecycle_source["terminal_event_type"],
                "terminal_event_effective_at_utc": lifecycle_source[
                    "terminal_event_effective_at_utc"
                ],
                "terminal_event_reason_code": lifecycle_source[
                    "terminal_event_reason_code"
                ],
                "lifecycle_source_observation_id": lifecycle_source[
                    "source_observation_id"
                ],
                "lifecycle_eligible_flag": lifecycle_flag,
                "model_data_eligible_flag": model_flag,
                "final_signal_eligible_flag": final_flag,
                "final_signal_exclusion_reason_codes": expected_reasons,
            }
        )
    minimums = plan_audit["domain_contract"]["cohort_minimum_cross_sections"]
    if any(eligible_by_cohort[cohort] < minimums[cohort] for cohort in minimums):
        raise ValueError("Consumer capture is below a registered cohort cross-section minimum")
    for cohort in sorted(expected_by_cohort):
        cohort_rows = sorted(
            (row for row in resolved_signals if row["group_id"] == cohort),
            key=lambda row: (int(row["rank"]), str(row["ticker"])),
        )
        eligible = [row for row in cohort_rows if row["eligible_flag"] == 1]
        count = max(1, math.ceil(0.20 * len(eligible)))
        top = {row["ticker"] for row in eligible[:count]}
        bottom = {row["ticker"] for row in eligible[-count:]}
        for row in cohort_rows:
            row["selected_top_flag"] = int(row["ticker"] in top)
            row["selected_bottom_flag"] = int(row["ticker"] in bottom)
    audit = {
        "registered_candidate_count": len(candidate_tickers),
        "eligible_count_by_cohort": eligible_by_cohort,
        "candidate_census_sha256": canonical_sha256(sorted(candidate_tickers)),
        "eligibility_resolution_sha256": canonical_sha256(resolution),
        "eligibility_policy": "lifecycle_eligible_intersect_frozen_model_data_eligible_v1",
        "exact_registered_candidate_census_pass": True,
        "exact_cohort_assignment_pass": True,
        "exact_membership_asof_pass": True,
        "no_discretionary_exclusion_pass": True,
        "independent_lifecycle_source_audit": lifecycle_audit,
        "membership_lifecycle_reconciled_pass": True,
    }
    return resolved_signals, audit


def validate_exact_registered_census(
    *,
    asof_date: str,
    signals: list[dict[str, Any]],
    membership_path: Path,
    plan_audit: Mapping[str, Any],
    score_replay_audit: Mapping[str, Any],
    lifecycle_snapshot_path: Path,
    lifecycle_attestation_path: Path,
    expected_lifecycle_attestation_sha256: str,
    trust_bundle: CanonicalTrustBundle,
    signal_cutoff_at_utc: str,
    membership_snapshot_bytes: bytes | None = None,
    lifecycle_snapshot_bytes: bytes | None = None,
    lifecycle_attestation_bytes: bytes | None = None,
) -> dict[str, Any]:
    _, audit = reconcile_exact_registered_census(
        asof_date=asof_date,
        signals=signals,
        membership_path=membership_path,
        plan_audit=plan_audit,
        score_replay_audit=score_replay_audit,
        lifecycle_snapshot_path=lifecycle_snapshot_path,
        lifecycle_attestation_path=lifecycle_attestation_path,
        expected_lifecycle_attestation_sha256=expected_lifecycle_attestation_sha256,
        trust_bundle=trust_bundle,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        membership_snapshot_bytes=membership_snapshot_bytes,
        lifecycle_snapshot_bytes=lifecycle_snapshot_bytes,
        lifecycle_attestation_bytes=lifecycle_attestation_bytes,
    )
    return audit


def capture_signal_v5(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_timestamp_receipt_path: Path,
    expected_registration_timestamp_receipt_sha256: str,
    evidence_public_key_path: Path,
    timestamp_public_key_path: Path,
    market_data_public_key_path: Path,
    asof_date: str,
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    capture_receipt_path: Path,
    expected_capture_receipt_sha256: str,
    capture_timestamp_receipt_path: Path,
    expected_capture_timestamp_receipt_sha256: str,
    archive_root: Path,
    previous_capture_path: Path | None = None,
) -> dict[str, Any]:
    asof = _exact_date(asof_date, label="Consumer capture asof")
    plan, contract, bundle, plan_audit = validate_registered_plan_v5(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_timestamp_receipt_path=registration_timestamp_receipt_path,
        expected_registration_timestamp_receipt_sha256=(
            expected_registration_timestamp_receipt_sha256
        ),
        evidence_public_key_path=evidence_public_key_path,
        timestamp_public_key_path=timestamp_public_key_path,
        market_data_public_key_path=market_data_public_key_path,
    )
    if set(plan_source_paths) != REQUIRED_PLAN_ROLES:
        raise ValueError("Consumer registered plan source roles changed")
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V5:
        raise ValueError("Consumer capture source roles changed")
    archived_paths, archive_audit = archive_source_set(
        capture_source_paths,
        expected_sha256=expected_capture_source_sha256,
        archive_root=archive_root,
        family="consumer_defensive",
        asof_date=asof,
    )
    capture_receipt_archive = archive_file_once(
        capture_receipt_path,
        expected_sha256=expected_capture_receipt_sha256,
        archive_root=archive_root,
        family="consumer_defensive",
        asof_date=asof,
        role="capture_receipt",
    )
    capture_timestamp_archive = archive_file_once(
        capture_timestamp_receipt_path,
        expected_sha256=expected_capture_timestamp_receipt_sha256,
        archive_root=archive_root,
        family="consumer_defensive",
        asof_date=asof,
        role="capture_timestamp_receipt",
    )
    archived_capture_receipt = Path(capture_receipt_archive["archive_path"])
    archived_capture_timestamp = Path(capture_timestamp_archive["archive_path"])
    source_snapshots = read_source_snapshots(archived_paths)
    source_hashes = {
        role: hashlib.sha256(payload).hexdigest()
        for role, payload in source_snapshots.items()
    }
    for role in REQUIRED_PLAN_ROLES:
        if (
            source_hashes[role]
            != plan_audit["domain_contract"]["registered_source_sha256"][role]
        ):
            raise ValueError(f"Consumer capture changed registered source: {role}")
    signals = derive_rank_signals(
        archived_paths["rank_snapshot"],
        asof_date=asof,
        rank_snapshot_bytes=source_snapshots["rank_snapshot"],
    )
    calendar_bytes = source_snapshots["trading_calendar"]
    calendar_rows, calendar_index = read_calendar_bytes(calendar_bytes)
    if asof not in calendar_index:
        raise ValueError("Consumer lifecycle asof is absent from the bound calendar")
    signal_cutoff_at_utc = calendar_rows[calendar_index[asof]][
        "exit_execution_at_utc"
    ]
    score_replay = validate_and_replay_consumer_scores(
        asof_date=asof,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        rank_snapshot_path=archived_paths["rank_snapshot"],
        feature_snapshot_path=archived_paths["atomic_feature_snapshot"],
        frozen_baseline_spec_path=archived_paths["frozen_baseline_spec"],
        score_input_availability_snapshot_path=archived_paths[
            "score_input_availability_snapshot"
        ],
        score_input_availability_attestation_path=archived_paths[
            "score_input_availability_attestation"
        ],
        expected_score_input_availability_attestation_sha256=source_hashes[
            "score_input_availability_attestation"
        ],
        canonical_trust_bundle=bundle,
        policy_id=POLICY_ID,
        expected_cohorts=sorted(COHORT_MINIMUMS),
        rank_snapshot_bytes=source_snapshots["rank_snapshot"],
        feature_snapshot_bytes=source_snapshots["atomic_feature_snapshot"],
        frozen_baseline_spec_bytes=source_snapshots["frozen_baseline_spec"],
        score_input_availability_snapshot_bytes=source_snapshots[
            "score_input_availability_snapshot"
        ],
        score_input_availability_attestation_bytes=source_snapshots[
            "score_input_availability_attestation"
        ],
    )
    frozen_contract = plan_audit["domain_contract"]["frozen_score_replay_contract"]
    if (
        score_replay.get("frozen_model_identity_sha256")
        != frozen_contract.get("model_identity_sha256")
        or score_replay.get("no_reestimation_from_outcomes_pass") is not True
    ):
        raise ValueError("Consumer replay differs from the registered frozen model")
    source_audit = validate_capture_sources_v5(
        asof_date=asof,
        source_paths=archived_paths,
        source_snapshot_bytes=source_snapshots,
    )
    signals, census_audit = reconcile_exact_registered_census(
        asof_date=asof,
        signals=signals,
        membership_path=archived_paths["membership_snapshot"],
        plan_audit=plan_audit,
        score_replay_audit=score_replay,
        lifecycle_snapshot_path=archived_paths["lifecycle_event_snapshot"],
        lifecycle_attestation_path=archived_paths["lifecycle_source_attestation"],
        expected_lifecycle_attestation_sha256=source_hashes[
            "lifecycle_source_attestation"
        ],
        trust_bundle=bundle,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        membership_snapshot_bytes=source_snapshots["membership_snapshot"],
        lifecycle_snapshot_bytes=source_snapshots["lifecycle_event_snapshot"],
        lifecycle_attestation_bytes=source_snapshots[
            "lifecycle_source_attestation"
        ],
    )
    capture_receipt_bytes = archived_capture_receipt.read_bytes()
    capture = build_strict_capture(
        contract=contract,
        asof_date=asof,
        signal_rows=signals,
        source_paths=archived_paths,
        expected_source_sha256=source_hashes,
        required_source_roles=REQUIRED_CAPTURE_ROLES_V5,
        trading_calendar_path=archived_paths["trading_calendar"],
        capture_receipt_path=archived_capture_receipt,
        expected_capture_receipt_sha256=expected_capture_receipt_sha256,
        authority=bundle.evidence_seal,
        source_snapshot_bytes=source_snapshots,
        trading_calendar_snapshot_bytes=calendar_bytes,
        capture_receipt_snapshot_bytes=capture_receipt_bytes,
        domain_fields={
            "domain_schema_version": CAPTURE_DOMAIN_SCHEMA,
            "domain_contract_sha256": plan_audit["domain_contract_sha256"],
            "registered_plan_sha256": plan_audit["registered_plan_sha256"],
            "baseline_state": plan["baseline_state"],
            "source_semantics_audit": source_audit,
            "frozen_score_replay_audit": score_replay,
            "registered_census_audit": census_audit,
            "content_addressed_archive_audit": archive_audit,
            "capture_receipt_archive_audit": capture_receipt_archive,
            "capture_timestamp_archive_audit": capture_timestamp_archive,
            "decision_window_policy": "first_n_nonoverlapping_once_v1",
            "terminal_event_tracking_required": True,
        },
    )
    validate_lifecycle_capture_chronology(
        census_audit["independent_lifecycle_source_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Consumer",
    )
    validate_score_input_availability_capture_chronology(
        score_replay["score_input_availability_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Consumer",
    )
    if previous_capture_path is None:
        predecessor = dict(plan_audit["registration_external_timestamp"])
    else:
        previous_payload, _, _, _ = read_json_snapshot(
            previous_capture_path,
            label="previous Consumer capture",
        )
        previous_sources, previous_receipt_bytes = _snapshot_capture_sources(
            previous_payload
        )
        previous_capture = validate_strict_capture(
            previous_payload,
            contract=contract,
            authority=bundle.evidence_seal,
            trading_calendar_path=archived_paths["trading_calendar"],
            source_snapshot_bytes=previous_sources,
            trading_calendar_snapshot_bytes=calendar_bytes,
            capture_receipt_snapshot_bytes=previous_receipt_bytes,
        )
        if (
            previous_capture.get("domain_schema_version") != CAPTURE_DOMAIN_SCHEMA
            or previous_capture.get("domain_contract_sha256")
            != plan_audit["domain_contract_sha256"]
        ):
            raise ValueError("previous Consumer capture changed the canonical domain")
        predecessor = revalidate_capture_timestamp(
            previous_capture,
            bundle=bundle,
            capture_receipt_snapshot_bytes=previous_receipt_bytes,
        )
        calendar_rows, _ = read_calendar_bytes(calendar_bytes)
        scheduled = scheduled_asofs(
            contract,
            calendar_rows=calendar_rows,
            complete_through_asof=asof,
        )
        if (
            len(scheduled) < 2
            or asof != scheduled[-1]
            or _exact_date(
                previous_capture["asof_date"],
                label="previous Consumer capture asof",
            )
            != scheduled[-2]
        ):
            raise ValueError("previous Consumer capture is not the immediately prior slot")
    if predecessor.get("external_timestamp_pass") is not True:
        raise ValueError("Consumer capture predecessor is not a validated external timestamp")
    if previous_capture_path is None and asof != contract.first_signal_date.isoformat():
        raise ValueError("only the first scheduled capture may follow registration directly")
    predecessor_sequence = predecessor.get("log_sequence")
    if type(predecessor_sequence) is not int or predecessor_sequence < 0:
        raise ValueError("Consumer predecessor log sequence must be a canonical integer")
    return attach_capture_timestamp(
        capture,
        capture_receipt_path=archived_capture_receipt,
        capture_timestamp_receipt_path=archived_capture_timestamp,
        expected_capture_timestamp_receipt_sha256=(
            expected_capture_timestamp_receipt_sha256
        ),
        expected_previous_log_head_sha256=str(
            predecessor["timestamp_receipt_sha256"]
        ),
        expected_previous_log_sequence=predecessor_sequence,
        expected_domain_contract_sha256=plan_audit["domain_contract_sha256"],
        bundle=bundle,
        capture_receipt_snapshot_bytes=capture_receipt_bytes,
    )


__all__ = [
    "CAPTURE_DOMAIN_SCHEMA",
    "LIFECYCLE_POLICY",
    "MEMBERSHIP_ROW_FIELDS_V5",
    "MEMBERSHIP_SCHEMA_V5",
    "MEMBERSHIP_TOP_LEVEL_FIELDS_V5",
    "REQUIRED_CAPTURE_ROLES_V5",
    "capture_signal_v5",
    "reconcile_exact_registered_census",
    "validate_capture_sources_v5",
    "validate_exact_registered_census",
]
