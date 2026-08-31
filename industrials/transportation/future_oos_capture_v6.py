"""Canonical Transportation v6 capture tied to the reviewed restart plan."""

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
from future_only_evidence.transport_score_input_availability import (
    validate_transport_score_input_availability_capture_chronology,
)

from .future_oos_activation_v6 import (
    GROUP_MODES,
    GROUP_MINIMUM_CROSS_SECTIONS,
    GROUP_TICKERS,
    GROUP_WEIGHTS,
    LIFECYCLE_EVENT_SCHEMA_V6,
    POLICY_ID,
    REQUIRED_PLAN_ROLES,
    SCORE_REPLAY_CONTRACT,
    validate_activation_plan_v6,
)
from .future_oos_capture_v4 import REQUIRED_CAPTURE_ROLES_V4
from .future_oos_capture_v5 import SOURCE_GENERATION_STATE, derive_transport_signals
from .future_oos_protocol_v1 import validate_fresh_sources
from .future_oos_score_lineage_v1 import (
    SOURCE_ROLES,
    validate_and_replay_transport_scores,
)


CAPTURE_DOMAIN_SCHEMA = "transportation_future_only_signal_capture_v6"
REQUIRED_CAPTURE_ROLES_V6 = frozenset(
    {
        *REQUIRED_CAPTURE_ROLES_V4,
        "universe_contract",
        "terminal_event_policy",
        "scoring_panel",
        "accepted_facts",
        "score_replay_baseline",
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
        "score_input_availability_snapshot",
        "score_input_availability_attestation",
        "lifecycle_event_snapshot",
        "lifecycle_source_attestation",
    }
)
MEMBERSHIP_SCHEMA_V6 = "transportation_future_membership_snapshot_v3"
MEMBERSHIP_TOP_LEVEL_FIELDS_V6 = frozenset(
    {"schema_version", "evidence_role", "asof_date", "rows", "rows_sha256"}
)
MEMBERSHIP_ROW_FIELDS_V6 = frozenset(
    {
        "asof_date",
        "ticker",
        "sleeve_id",
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
        raise ValueError("previous Transportation capture lacks source/receipt identities")
    paths: dict[str, Path] = {}
    for role, identity in identities.items():
        if not isinstance(identity, dict):
            raise ValueError("previous Transportation source identity is invalid")
        paths[str(role)] = Path(str(identity.get("path") or ""))
    receipt_bytes = (
        Path(str(receipt.get("path") or "")).expanduser().resolve().read_bytes()
    )
    return read_source_snapshots(paths), receipt_bytes


def _membership_rows(
    path: Path,
    *,
    asof_date: str,
    snapshot_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    resolved = Path(path)
    if resolved.suffix.lower() != ".json":
        raise ValueError("Transportation v6 membership must use canonical hash-bound JSON")
    payload_bytes = (
        bytes(snapshot_bytes)
        if snapshot_bytes is not None
        else resolved.expanduser().resolve().read_bytes()
    )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Transportation membership must be valid UTF-8 JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != MEMBERSHIP_TOP_LEVEL_FIELDS_V6
        or payload.get("schema_version") != MEMBERSHIP_SCHEMA_V6
        or payload.get("evidence_role") != PROSPECTIVE_ROLE
        or str(payload.get("asof_date") or "") != asof_date
    ):
        raise ValueError("unsupported Transportation v6 membership identity/asof")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("Transportation membership rows are hash-inconsistent")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != MEMBERSHIP_ROW_FIELDS_V6:
            raise ValueError("Transportation membership row field census changed")
        if row["asof_date"] != asof_date:
            raise ValueError("Transportation membership row asof differs from capture")
        result.append(dict(row))
    return result


def validate_exact_transport_census(
    *,
    signals: list[dict[str, Any]],
    membership_path: Path,
    asof_date: str,
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
    asof = _exact_date(asof_date, label="Transportation membership asof")
    expected_by_group = {group: set(tickers) for group, tickers in GROUP_TICKERS.items()}
    signal_by_group: dict[str, set[str]] = {group: set() for group in expected_by_group}
    signal_index: dict[str, dict[str, Any]] = {}
    for signal in signals:
        ticker = str(signal["ticker"])
        group = str(signal["group_id"])
        if ticker in signal_index or group not in signal_by_group:
            raise ValueError("Transportation signal ticker/group census is invalid")
        signal_index[ticker] = signal
        signal_by_group[group].add(ticker)
    if signal_by_group != expected_by_group:
        raise ValueError("Transportation capture changed the frozen within-group ticker census")
    rows = _membership_rows(
        membership_path,
        asof_date=asof,
        snapshot_bytes=membership_snapshot_bytes,
    )
    membership: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = row.get("ticker")
        if (
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            or ticker in membership
        ):
            raise ValueError("Transportation membership ticker census is invalid")
        membership[ticker] = row
    if set(membership) != set(signal_index):
        raise ValueError("Transportation membership differs from frozen policy ticker census")
    replay_index = score_replay_audit.get("model_data_eligibility_by_ticker")
    if not isinstance(replay_index, dict) or set(replay_index) != set(signal_index):
        raise ValueError("Transportation replay eligibility census differs from policy")
    lifecycle_index, lifecycle_audit = validate_lifecycle_event_snapshot(
        lifecycle_snapshot_path,
        expected_schema_version=LIFECYCLE_EVENT_SCHEMA_V6,
        asof_date=asof,
        expected_tickers=signal_index,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        family="transportation",
        policy_id=POLICY_ID,
        attestation_path=lifecycle_attestation_path,
        expected_attestation_sha256=expected_lifecycle_attestation_sha256,
        bundle=trust_bundle,
        snapshot_bytes=lifecycle_snapshot_bytes,
        attestation_bytes=lifecycle_attestation_bytes,
    )
    eligible_by_group = {group: 0 for group in expected_by_group}
    resolution: list[dict[str, Any]] = []
    resolved_index: dict[str, dict[str, Any]] = {}
    for ticker in sorted(signal_index):
        signal = signal_index[ticker]
        row = membership[ticker]
        if (
            str(row["sleeve_id"]) != signal["sleeve_id"]
            or str(row["group_id"]) != signal["group_id"]
        ):
            raise ValueError(f"{ticker}: Transportation lifecycle/group identity mismatch")
        lifecycle_source = lifecycle_index[ticker]
        lifecycle = str(lifecycle_source["lifecycle_status_at_signal_cutoff"])
        if row["lifecycle_status_at_signal_cutoff"] != lifecycle:
            raise ValueError(
                f"{ticker}: membership lifecycle differs from independent source"
            )
        if lifecycle not in LIFECYCLE_POLICY:
            raise ValueError(f"{ticker}: Transportation lifecycle is outside policy")
        expected_lifecycle, lifecycle_reason = LIFECYCLE_POLICY[lifecycle]
        lifecycle_flag = _strict_flag(
            row["lifecycle_eligible_flag"], label=f"{ticker} lifecycle eligibility"
        )
        if lifecycle_flag != expected_lifecycle:
            raise ValueError(f"{ticker}: lifecycle eligibility differs from policy")
        replay = replay_index[ticker]
        if not isinstance(replay, dict):
            raise ValueError(f"{ticker}: Transportation replay eligibility is invalid")
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
            raise ValueError(f"{ticker}: Transportation replay reason census is invalid")
        if (
            _strict_flag(
                row["model_data_eligible_flag"],
                label=f"{ticker} membership model/data eligibility",
            )
            != model_flag
            or row["model_data_exclusion_reason_codes"] != model_reasons
        ):
            raise ValueError(f"{ticker}: membership differs from deterministic replay")
        if _strict_flag(
            signal["eligible_flag"], label=f"{ticker} source model eligibility"
        ) != model_flag:
            raise ValueError(f"{ticker}: score eligibility differs from deterministic replay")
        final_flag = lifecycle_flag & model_flag
        expected_reasons = sorted(
            set(([lifecycle_reason] if lifecycle_reason else []) + list(model_reasons))
        )
        if (
            _strict_flag(
                row["final_signal_eligible_flag"],
                label=f"{ticker} final signal eligibility",
            )
            != final_flag
            or row["final_signal_exclusion_reason_codes"] != expected_reasons
            or (final_flag == 1 and expected_reasons)
            or (final_flag == 0 and not expected_reasons)
        ):
            raise ValueError(
                f"{ticker}: final eligibility is not lifecycle intersect model/data"
            )
        resolved_index[ticker] = {
            **signal,
            "eligible_flag": final_flag,
            "predictive_eligible_flag": int(
                final_flag and signal["ranking_mode"] == "ranked"
            ),
            "lifecycle_eligible_flag": lifecycle_flag,
            "model_data_eligible_flag": model_flag,
            "final_signal_exclusion_reason_codes": expected_reasons,
        }
        eligible_by_group[signal["group_id"]] += final_flag
        resolution.append(
            {
                "ticker": ticker,
                "group_id": signal["group_id"],
                "lifecycle_status_at_signal_cutoff": lifecycle,
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
    if any(
        eligible_by_group[group] < GROUP_MINIMUM_CROSS_SECTIONS[group]
        for group in GROUP_MINIMUM_CROSS_SECTIONS
    ):
        raise ValueError("Transportation group is below its frozen minimum cross-section")
    for group in sorted(expected_by_group):
        ordered = sorted(
            (resolved_index[ticker] for ticker in expected_by_group[group]),
            key=lambda row: (int(row["rank"]), str(row["ticker"])),
        )
        eligible = [row for row in ordered if row["eligible_flag"] == 1]
        count = max(1, math.ceil(0.20 * len(eligible))) if eligible else 0
        top = {row["ticker"] for row in eligible[:count]}
        bottom = {row["ticker"] for row in eligible[-count:]}
        if GROUP_MODES[group] == "eligibility_equal_weight":
            top = bottom = set()
        for signal in ordered:
            signal["selected_top_flag"] = int(signal["ticker"] in top)
            signal["selected_bottom_flag"] = int(signal["ticker"] in bottom)
    audit = {
        "ticker_count": len(signal_index),
        "eligible_count_by_group": eligible_by_group,
        "ticker_census_sha256": canonical_sha256(sorted(signal_index)),
        "exact_policy_ticker_census_pass": True,
        "eligibility_resolution_sha256": canonical_sha256(resolution),
        "eligibility_policy": "lifecycle_eligible_intersect_frozen_model_data_eligible_v1",
        "exact_membership_asof_pass": True,
        "no_discretionary_exclusion_pass": True,
        "independent_lifecycle_source_audit": lifecycle_audit,
        "membership_lifecycle_reconciled_pass": True,
    }
    return list(resolved_index.values()), audit


def capture_signal_v6(
    *,
    activation_plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    activation_receipt_path: Path,
    expected_activation_receipt_sha256: str,
    activation_timestamp_receipt_path: Path,
    expected_activation_timestamp_receipt_sha256: str,
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
    asof = _exact_date(asof_date, label="Transportation capture asof")
    plan, contract, bundle, plan_audit = validate_activation_plan_v6(
        activation_plan_path,
        source_paths=plan_source_paths,
        activation_receipt_path=activation_receipt_path,
        expected_activation_receipt_sha256=expected_activation_receipt_sha256,
        activation_timestamp_receipt_path=activation_timestamp_receipt_path,
        expected_activation_timestamp_receipt_sha256=(
            expected_activation_timestamp_receipt_sha256
        ),
        evidence_public_key_path=evidence_public_key_path,
        timestamp_public_key_path=timestamp_public_key_path,
        market_data_public_key_path=market_data_public_key_path,
    )
    if set(plan_source_paths) != REQUIRED_PLAN_ROLES:
        raise ValueError("Transportation plan source roles changed")
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V6:
        raise ValueError("Transportation capture source role census changed")
    archived_paths, archive_audit = archive_source_set(
        capture_source_paths,
        expected_sha256=expected_capture_source_sha256,
        archive_root=archive_root,
        family="transportation",
        asof_date=asof,
    )
    capture_receipt_archive = archive_file_once(
        capture_receipt_path,
        expected_sha256=expected_capture_receipt_sha256,
        archive_root=archive_root,
        family="transportation",
        asof_date=asof,
        role="capture_receipt",
    )
    capture_timestamp_archive = archive_file_once(
        capture_timestamp_receipt_path,
        expected_sha256=expected_capture_timestamp_receipt_sha256,
        archive_root=archive_root,
        family="transportation",
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
            raise ValueError(f"Transportation capture changed registered source: {role}")
    calendar_bytes = source_snapshots["trading_calendar"]
    calendar_rows, calendar_index = read_calendar_bytes(calendar_bytes)
    if asof not in calendar_index:
        raise ValueError("Transportation lifecycle asof is absent from the bound calendar")
    signal_cutoff_at_utc = calendar_rows[calendar_index[asof]][
        "exit_execution_at_utc"
    ]
    scheduled = scheduled_asofs(
        contract,
        calendar_rows=calendar_rows,
        complete_through_asof=asof,
    )
    if not scheduled or asof != scheduled[-1]:
        raise ValueError("Transportation capture is outside the frozen schedule")
    predecessor_replay_audit: Mapping[str, Any] | None = None
    predecessor_availability_audit: Mapping[str, Any]
    if previous_capture_path is None:
        predecessor = plan_audit["activation_external_timestamp"]
        predecessor_availability_audit = plan_audit["domain_contract"][
            "score_input_availability_baseline_audit"
        ]
        if (
            asof != contract.first_signal_date.isoformat()
            or scheduled != [asof]
        ):
            raise ValueError("only the first Transportation slot may follow activation")
    else:
        previous_capture, _, _, _ = read_json_snapshot(
            previous_capture_path,
            label="previous Transportation capture",
        )
        previous_source_snapshots, previous_receipt_bytes = (
            _snapshot_capture_sources(previous_capture)
        )
        previous_capture = validate_strict_capture(
            previous_capture,
            contract=contract,
            authority=bundle.evidence_seal,
            trading_calendar_path=archived_paths["trading_calendar"],
            source_snapshot_bytes=previous_source_snapshots,
            trading_calendar_snapshot_bytes=previous_source_snapshots[
                "trading_calendar"
            ],
            capture_receipt_snapshot_bytes=previous_receipt_bytes,
        )
        if (
            previous_capture.get("domain_schema_version")
            != CAPTURE_DOMAIN_SCHEMA
            or previous_capture.get("domain_contract_sha256")
            != plan_audit["domain_contract_sha256"]
            or previous_capture.get("activation_plan_sha256")
            != plan_audit["registered_plan_sha256"]
        ):
            raise ValueError("previous Transportation capture changed the domain")
        if (
            len(scheduled) < 2
            or _exact_date(
                previous_capture["asof_date"],
                label="previous Transportation capture asof",
            )
            != scheduled[-2]
        ):
            raise ValueError("previous Transportation capture is not the prior slot")
        predecessor_replay_audit = previous_capture.get(
            "frozen_score_replay_audit"
        )
        if not isinstance(predecessor_replay_audit, Mapping):
            raise ValueError("previous Transportation replay audit is absent")
        previous_availability = predecessor_replay_audit.get(
            "score_input_availability_audit"
        )
        if not isinstance(previous_availability, Mapping):
            raise ValueError(
                "previous Transportation score-input availability audit is absent"
            )
        predecessor_availability_audit = previous_availability
        predecessor = revalidate_capture_timestamp(
            previous_capture,
            bundle=bundle,
            capture_receipt_snapshot_bytes=previous_receipt_bytes,
        )
    score_rows, rank_rows, manifest = validate_fresh_sources(
        asof_date=asof,
        capture_date=asof,
        score_path=archived_paths["canonical_v8_score"],
        rank_path=archived_paths["canonical_v8_rank"],
        source_manifest_path=archived_paths["source_manifest"],
        source_snapshot_bytes={
            role: source_snapshots[role]
            for role in (
                "canonical_v8_score",
                "canonical_v8_rank",
                "source_manifest",
            )
        },
    )
    if (
        manifest.get("evidence_role") != PROSPECTIVE_ROLE
        or manifest.get("source_generation_state") != SOURCE_GENERATION_STATE
        or manifest.get("return_target")
        != {
            "benchmark_ticker": "IYT",
            "return_convention": "next_session_open_execution_total_return_v1",
            "target_field": "forward_iyt_excess_return_at_fixed_session_horizon",
        }
    ):
        raise ValueError("Transportation source manifest changed prospective target/state")
    signals, coverage = derive_transport_signals(
        score_rows=score_rows,
        rank_rows=rank_rows,
        group_weights=GROUP_WEIGHTS,
        group_modes=GROUP_MODES,
        asof_date=asof,
    )
    replay_roles = set(SOURCE_ROLES)
    score_replay = validate_and_replay_transport_scores(
        asof_date=asof,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        scheduled_append_asof_dates=scheduled,
        score_path=archived_paths["canonical_v8_score"],
        scoring_panel_path=archived_paths["scoring_panel"],
        accepted_facts_path=archived_paths["accepted_facts"],
        score_replay_baseline_path=archived_paths["score_replay_baseline"],
        score_input_availability_baseline_snapshot_path=archived_paths[
            "score_input_availability_baseline_snapshot"
        ],
        score_input_availability_baseline_attestation_path=archived_paths[
            "score_input_availability_baseline_attestation"
        ],
        score_input_availability_snapshot_path=archived_paths[
            "score_input_availability_snapshot"
        ],
        score_input_availability_attestation_path=archived_paths[
            "score_input_availability_attestation"
        ],
        v8_policy_path=archived_paths["v8_policy"],
        policy_id=POLICY_ID,
        canonical_trust_bundle=bundle,
        expected_sha256={role: source_hashes[role] for role in replay_roles},
        predecessor_replay_audit=predecessor_replay_audit,
        predecessor_score_input_availability_audit=(
            predecessor_availability_audit
        ),
        source_snapshot_bytes={
            role: source_snapshots[role] for role in replay_roles
        },
    )
    if {
        field: score_replay.get(field) for field in SCORE_REPLAY_CONTRACT
    } != SCORE_REPLAY_CONTRACT:
        raise ValueError("Transportation score replay changed the activation contract")
    signals, census_audit = validate_exact_transport_census(
        signals=signals,
        membership_path=archived_paths["membership_snapshot"],
        asof_date=asof,
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
    if coverage.get("oil_tanker_operators_v5") is not True:
        raise ValueError("Transportation tanker specialized-coverage gate failed")
    capture_receipt_bytes = archived_capture_receipt.read_bytes()
    capture = build_strict_capture(
        contract=contract,
        asof_date=asof,
        signal_rows=signals,
        source_paths=archived_paths,
        expected_source_sha256=source_hashes,
        required_source_roles=REQUIRED_CAPTURE_ROLES_V6,
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
            "activation_plan_sha256": plan_audit["registered_plan_sha256"],
            "frozen_group_weights": GROUP_WEIGHTS,
            "group_ranking_modes": GROUP_MODES,
            "group_ticker_census": GROUP_TICKERS,
            "registered_census_audit": census_audit,
            "frozen_score_replay_audit": score_replay,
            "content_addressed_archive_audit": archive_audit,
            "capture_receipt_archive_audit": capture_receipt_archive,
            "capture_timestamp_archive_audit": capture_timestamp_archive,
            "sleeve_coverage_gates": coverage,
            "parcel_predictive_applicability": "not_applicable_monitor_only",
            "decision_window_policy": "first_n_nonoverlapping_once_v1",
        },
    )
    validate_lifecycle_capture_chronology(
        census_audit["independent_lifecycle_source_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Transportation",
    )
    validate_transport_score_input_availability_capture_chronology(
        score_replay["score_input_availability_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Transportation",
    )
    predecessor_sequence = predecessor.get("log_sequence")
    if type(predecessor_sequence) is not int or predecessor_sequence < 0:
        raise ValueError(
            "Transportation predecessor log sequence must be a canonical integer"
        )
    return attach_capture_timestamp(
        capture,
        capture_receipt_path=archived_capture_receipt,
        capture_timestamp_receipt_path=archived_capture_timestamp,
        expected_capture_timestamp_receipt_sha256=(
            expected_capture_timestamp_receipt_sha256
        ),
        expected_previous_log_head_sha256=str(predecessor["timestamp_receipt_sha256"]),
        expected_previous_log_sequence=predecessor_sequence,
        expected_domain_contract_sha256=plan_audit["domain_contract_sha256"],
        bundle=bundle,
        capture_receipt_snapshot_bytes=capture_receipt_bytes,
    )


__all__ = [
    "CAPTURE_DOMAIN_SCHEMA",
    "MEMBERSHIP_ROW_FIELDS_V6",
    "MEMBERSHIP_SCHEMA_V6",
    "MEMBERSHIP_TOP_LEVEL_FIELDS_V6",
    "REQUIRED_CAPTURE_ROLES_V6",
    "capture_signal_v6",
    "validate_exact_transport_census",
]
