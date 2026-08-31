"""Signed point-in-time provenance for every Transportation v8 score input.

The Transportation replay consumes two cumulative input ledgers: scoring-panel
rows (generic metrics, positioning, and readiness flags) and accepted facts
(specialized metrics).  Content sealing alone cannot prove that either ledger
represented information available at the official-close signal cutoff.  This
module requires an independently signed, exact crosswalk from every input row
to an allowlisted provider record and its availability time.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical_trust import CanonicalTrustBundle
from .canonical_values import exact_utc
from .protocol import canonical_sha256
from .prospective_contracts import PROSPECTIVE_ROLE


TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA = (
    "transportation_future_v8_score_input_availability_snapshot_v1"
)
TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA = (
    "transportation_future_v8_score_input_availability_attestation_v1"
)
TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY = (
    "complete_panel_and_fact_state_official_close_pit_v1"
)
PANEL_INPUT_KIND = "scoring_panel_row"
FACT_INPUT_KIND = "accepted_fact_row"
ACTIVATION_BASELINE_ROLE = "prospective_future_only_activation_baseline"

AVAILABILITY_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "snapshot_generated_at_utc",
        "panel_input_rows",
        "panel_input_rows_sha256",
        "accepted_fact_input_rows",
        "accepted_fact_input_rows_sha256",
    }
)
AVAILABILITY_ROW_FIELDS = frozenset(
    {
        "input_kind",
        "ticker",
        "input_identity_sha256",
        "input_content_sha256",
        "input_value_sha256",
        "availability_status",
        "source_required_flag",
        "source_id",
        "source_available_at_utc",
        "source_observation_id",
        "source_locator",
        "source_record_sha256",
        "provider_id",
        "dataset_id",
    }
)
AVAILABILITY_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "authority_id",
        "signature_base64",
        "signed_payload_sha256",
        "family",
        "policy_id",
        "evidence_role",
        "asof_date",
        "availability_snapshot_sha256",
        "panel_input_rows_sha256",
        "accepted_fact_input_rows_sha256",
        "input_count",
        "panel_input_count",
        "accepted_fact_input_count",
        "input_identity_census_sha256",
        "input_content_census_sha256",
        "input_value_census_sha256",
        "source_observation_mapping_sha256",
        "provider_dataset_pair_count",
        "provider_dataset_pairs_sha256",
        "source_max_information_at_utc",
        "status_effective_through_at_utc",
        "exported_at_utc",
        "status_asof_policy",
        "query_sha256",
    }
)


def transport_score_input_availability_contract() -> dict[str, Any]:
    return {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "attestation_schema": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "evidence_roles": sorted({PROSPECTIVE_ROLE, ACTIVATION_BASELINE_ROLE}),
        "top_level_fields": sorted(AVAILABILITY_TOP_LEVEL_FIELDS),
        "row_fields": sorted(AVAILABILITY_ROW_FIELDS),
        "status_asof_policy": TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
        "source_authority_role": "pinned_market_data_export_ed25519_authority",
        "input_census": (
            "exact_ordered_complete_scoring_panel_and_accepted_fact_rows_v1"
        ),
        "input_content_policy": (
            "full_row_and_score_driving_value_sha256_bound_v1"
        ),
        "source_record_reuse_policy": (
            "same_observation_only_with_identical_provenance_and_ticker_v1"
        ),
        "negative_input_policy": (
            "exact_empty_fact_census_and_attested_panel_status_fields_v1"
        ),
    }


def _exact_date(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return value


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _canonical_text(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a nonblank canonical string")
    return value


def _canonical_ticker(value: Any, *, label: str) -> str:
    ticker = _canonical_text(value, label=label)
    if ticker.upper() != ticker:
        raise ValueError(f"{label} must be canonical uppercase")
    return ticker


def _strict_sha256(value: Any, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be exact lowercase SHA-256")
    return value


def _strict_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be a canonical integer >= {minimum}")
    return value


def _finite_number(value: Any, *, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite canonical JSON number")
    return float(value)


def _read_bytes(path: Path, supplied: bytes | None) -> bytes:
    return (
        bytes(supplied)
        if supplied is not None
        else Path(path).expanduser().resolve().read_bytes()
    )


def _json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def transport_panel_input_identity(row: Mapping[str, Any]) -> str:
    ticker = _canonical_ticker(row.get("ticker"), label="panel input ticker")
    score_asof = _exact_date(row.get("asof_date"), label=f"{ticker} panel asof")
    horizon = _strict_int(
        row.get("horizon_sessions"),
        label=f"{score_asof}/{ticker} panel horizon",
        minimum=1,
    )
    return canonical_sha256(
        {
            "input_kind": PANEL_INPUT_KIND,
            "asof_date": score_asof,
            "ticker": ticker,
            "horizon_sessions": horizon,
        }
    )


def transport_panel_input_value_sha256(row: Mapping[str, Any]) -> str:
    """Bind every panel field that can change a replayed score or eligibility."""

    return canonical_sha256(
        {
            "metric_values_json": row.get("metric_values_json"),
            "metric_status_json": row.get("metric_status_json"),
            "positioning_score": row.get("positioning_score"),
            "rank_ready_flag": row.get("rank_ready_flag"),
            "calibration_eligible_flag": row.get("calibration_eligible_flag"),
            "calibration_cohort": row.get("calibration_cohort"),
            "source_score_sha256": row.get("source_score_sha256"),
        }
    )


def transport_fact_identity(row: Mapping[str, Any]) -> str:
    """Mirror the frozen v8 accepted-fact identity exactly."""

    ticker = _canonical_ticker(row.get("ticker"), label="accepted fact ticker")
    metric_id = _canonical_text(
        row.get("metric_id"), label=f"{ticker} accepted fact metric"
    )
    period_end = _exact_date(
        row.get("period_end"), label=f"{ticker}/{metric_id} fact period end"
    )
    source_identity = row.get("candidate_key") or row.get("evidence_key")
    _canonical_text(
        source_identity,
        label=f"{ticker}/{metric_id} accepted fact source identity",
    )
    return canonical_sha256(
        {
            "ticker": ticker,
            "metric_id": metric_id,
            "period_end": period_end,
            "candidate_key": row.get("candidate_key"),
            "evidence_key": row.get("evidence_key"),
            "source_content_sha256": row.get("source_content_sha256"),
        }
    )


def transport_fact_input_value_sha256(row: Mapping[str, Any]) -> str:
    ticker = _canonical_ticker(row.get("ticker"), label="accepted fact ticker")
    metric_id = _canonical_text(
        row.get("metric_id"), label=f"{ticker} accepted fact metric"
    )
    _finite_number(row.get("value"), label=f"{ticker}/{metric_id} fact value")
    _canonical_text(row.get("unit"), label=f"{ticker}/{metric_id} fact unit")
    return canonical_sha256(
        {
            "ticker": ticker,
            "metric_id": metric_id,
            "period_end": row.get("period_end"),
            "filing_date": row.get("filing_date"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "candidate_key": row.get("candidate_key"),
            "evidence_key": row.get("evidence_key"),
            "source_content_sha256": row.get("source_content_sha256"),
        }
    )


def _expected_inputs(
    panel_rows: Sequence[Mapping[str, Any]],
    fact_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if isinstance(panel_rows, (str, bytes)) or not panel_rows:
        raise ValueError("Transportation availability requires panel inputs")
    if isinstance(fact_rows, (str, bytes)):
        raise ValueError("Transportation accepted-fact inputs must be a sequence")
    panel: list[dict[str, str]] = []
    facts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in panel_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("Transportation expected panel input is malformed")
        row = dict(raw)
        identity = transport_panel_input_identity(row)
        key = (PANEL_INPUT_KIND, identity)
        if key in seen:
            raise ValueError("Transportation expected panel input identity is duplicated")
        seen.add(key)
        panel.append(
            {
                "ticker": str(row["ticker"]),
                "identity": identity,
                "content": canonical_sha256(row),
                "value": transport_panel_input_value_sha256(row),
            }
        )
    for raw in fact_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("Transportation expected accepted fact is malformed")
        row = dict(raw)
        identity = transport_fact_identity(row)
        key = (FACT_INPUT_KIND, identity)
        if key in seen:
            raise ValueError("Transportation expected fact input identity is duplicated")
        seen.add(key)
        facts.append(
            {
                "ticker": str(row["ticker"]),
                "identity": identity,
                "content": canonical_sha256(row),
                "value": transport_fact_input_value_sha256(row),
            }
        )
    return panel, facts


def _validate_availability_rows(
    raw_rows: Any,
    *,
    expected: Sequence[Mapping[str, str]],
    input_kind: str,
    asof: str,
    generated: datetime,
    cutoff: datetime,
    bundle: CanonicalTrustBundle,
    shared_records: dict[str, tuple[str, ...]],
) -> tuple[list[dict[str, Any]], list[datetime], list[dict[str, Any]]]:
    if not isinstance(raw_rows, list) or len(raw_rows) != len(expected):
        raise ValueError(
            f"Transportation {input_kind} availability census is not exact"
        )
    parsed: list[dict[str, Any]] = []
    times: list[datetime] = []
    mappings: list[dict[str, Any]] = []
    for position, (raw, expected_row) in enumerate(zip(raw_rows, expected)):
        if not isinstance(raw, dict) or set(raw) != AVAILABILITY_ROW_FIELDS:
            raise ValueError("Transportation score-input availability row census changed")
        row = dict(raw)
        if row.get("input_kind") != input_kind:
            raise ValueError("Transportation score-input availability identity changed")
        ticker = _canonical_ticker(
            row.get("ticker"), label=f"{input_kind} availability ticker"
        )
        identity = _strict_sha256(
            row.get("input_identity_sha256"),
            label=f"{input_kind} input identity",
        )
        content = _strict_sha256(
            row.get("input_content_sha256"),
            label=f"{input_kind} input content sha256",
        )
        value_digest = _strict_sha256(
            row.get("input_value_sha256"),
            label=f"{input_kind} input value sha256",
        )
        if (
            ticker != expected_row["ticker"]
            or identity != expected_row["identity"]
            or content != expected_row["content"]
            or value_digest != expected_row["value"]
        ):
            raise ValueError(
                f"Transportation {input_kind} availability differs from input at position {position}"
            )
        if row.get("availability_status") != "available":
            raise ValueError("Transportation score-driving input is not attested available")
        if type(row.get("source_required_flag")) is not int or row.get(
            "source_required_flag"
        ) != 1:
            raise ValueError("Transportation score-driving input requires one source record")
        source_id = _canonical_text(
            row.get("source_id"), label=f"{ticker} source id"
        )
        available = _utc(
            row.get("source_available_at_utc"),
            label=f"{ticker} source availability",
        )
        observation_id = _canonical_text(
            row.get("source_observation_id"),
            label=f"{ticker} source observation id",
        )
        locator = _canonical_text(
            row.get("source_locator"), label=f"{ticker} source locator"
        )
        record_sha = _strict_sha256(
            row.get("source_record_sha256"),
            label=f"{ticker} source record sha256",
        )
        provider = _canonical_text(
            row.get("provider_id"), label=f"{ticker} provider id"
        )
        dataset = _canonical_text(
            row.get("dataset_id"), label=f"{ticker} dataset id"
        )
        if (
            provider not in bundle.allowed_provider_ids
            or dataset not in bundle.allowed_dataset_ids
        ):
            raise ValueError(
                "Transportation score-input provider/dataset is outside trust allowlists"
            )
        if available > cutoff or available > generated:
            raise ValueError("Transportation score input was available after cutoff")
        provenance = (
            ticker,
            provider,
            dataset,
            source_id,
            available.isoformat(),
            locator,
            record_sha,
        )
        previous = shared_records.setdefault(observation_id, provenance)
        if previous != provenance:
            raise ValueError(
                "Transportation reused source observation has inconsistent provenance"
            )
        mapping = {
            "input_kind": input_kind,
            "ticker": ticker,
            "input_identity_sha256": identity,
            "input_content_sha256": content,
            "input_value_sha256": value_digest,
            "source_observation_id": observation_id,
            "source_record_sha256": record_sha,
            "source_available_at_utc": available.isoformat(),
            "source_locator": locator,
            "source_id": source_id,
            "provider_id": provider,
            "dataset_id": dataset,
        }
        parsed.append(row)
        times.append(available)
        mappings.append(mapping)
    return parsed, times, mappings


def validate_transport_score_input_availability_snapshot(
    path: Path,
    *,
    asof_date: str,
    expected_panel_rows: Sequence[Mapping[str, Any]],
    expected_accepted_fact_rows: Sequence[Mapping[str, Any]],
    signal_cutoff_at_utc: str,
    policy_id: str,
    attestation_path: Path,
    expected_attestation_sha256: str,
    bundle: CanonicalTrustBundle,
    expected_evidence_role: str = PROSPECTIVE_ROLE,
    predecessor_availability_audit: Mapping[str, Any] | None = None,
    snapshot_bytes: bytes | None = None,
    attestation_bytes: bytes | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Verify the exact signed availability census for all v8 replay inputs."""

    asof = _exact_date(asof_date, label="Transportation availability asof")
    cutoff = _utc(
        signal_cutoff_at_utc, label="Transportation availability signal cutoff"
    )
    if cutoff.date().isoformat() != asof:
        raise ValueError("Transportation availability cutoff date differs from asof")
    policy = _canonical_text(policy_id, label="Transportation availability policy id")
    evidence_role = _canonical_text(
        expected_evidence_role,
        label="Transportation availability evidence role",
    )
    if evidence_role not in {PROSPECTIVE_ROLE, ACTIVATION_BASELINE_ROLE}:
        raise ValueError("Transportation availability evidence role is unsupported")
    expected_panel, expected_facts = _expected_inputs(
        expected_panel_rows, expected_accepted_fact_rows
    )

    payload_bytes = _read_bytes(path, snapshot_bytes)
    payload = _json(payload_bytes, label="Transportation score-input availability")
    if set(payload) != AVAILABILITY_TOP_LEVEL_FIELDS:
        raise ValueError("Transportation availability top-level census changed")
    if (
        payload.get("schema_version") != TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA
        or payload.get("evidence_role") != evidence_role
        or payload.get("asof_date") != asof
    ):
        raise ValueError("Transportation availability identity/asof changed")
    generated = _utc(
        payload.get("snapshot_generated_at_utc"),
        label="Transportation availability snapshot generation time",
    )
    raw_panel = payload.get("panel_input_rows")
    raw_facts = payload.get("accepted_fact_input_rows")
    if (
        not isinstance(raw_panel, list)
        or payload.get("panel_input_rows_sha256") != canonical_sha256(raw_panel)
        or not isinstance(raw_facts, list)
        or payload.get("accepted_fact_input_rows_sha256")
        != canonical_sha256(raw_facts)
    ):
        raise ValueError("Transportation availability rows are hash-inconsistent")
    shared_records: dict[str, tuple[str, ...]] = {}
    panel_rows, panel_times, panel_mappings = _validate_availability_rows(
        raw_panel,
        expected=expected_panel,
        input_kind=PANEL_INPUT_KIND,
        asof=asof,
        generated=generated,
        cutoff=cutoff,
        bundle=bundle,
        shared_records=shared_records,
    )
    fact_rows, fact_times, fact_mappings = _validate_availability_rows(
        raw_facts,
        expected=expected_facts,
        input_kind=FACT_INPUT_KIND,
        asof=asof,
        generated=generated,
        cutoff=cutoff,
        bundle=bundle,
        shared_records=shared_records,
    )

    snapshot_sha = hashlib.sha256(payload_bytes).hexdigest()
    receipt_bytes = _read_bytes(attestation_path, attestation_bytes)
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_sha != _strict_sha256(
        expected_attestation_sha256,
        label="Transportation availability attestation sha256",
    ):
        raise ValueError("Transportation availability attestation SHA-256 mismatch")
    attestation = _json(
        receipt_bytes, label="Transportation score-input availability attestation"
    )
    if set(attestation) != AVAILABILITY_ATTESTATION_FIELDS:
        raise ValueError("Transportation availability attestation census changed")
    bundle.market_data_export.verify_snapshot(
        receipt_bytes, receipt_sha, attestation
    )
    exported = _utc(
        attestation.get("exported_at_utc"),
        label="Transportation availability export time",
    )
    effective_through = _utc(
        attestation.get("status_effective_through_at_utc"),
        label="Transportation availability effective-through time",
    )

    identity_census = {
        "panel": [row["identity"] for row in expected_panel],
        "accepted_facts": [row["identity"] for row in expected_facts],
    }
    content_census = {
        "panel": [row["content"] for row in expected_panel],
        "accepted_facts": [row["content"] for row in expected_facts],
    }
    value_census = {
        "panel": [row["value"] for row in expected_panel],
        "accepted_facts": [row["value"] for row in expected_facts],
    }
    mappings = panel_mappings + fact_mappings
    pairs = sorted(
        {
            (str(row["provider_id"]), str(row["dataset_id"]))
            for row in panel_rows + fact_rows
        }
    )
    pair_payload = [
        {"provider_id": provider, "dataset_id": dataset}
        for provider, dataset in pairs
    ]
    all_times = panel_times + fact_times
    max_available = max(all_times)
    expected_claims = {
        "schema_version": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "family": "transportation",
        "policy_id": policy,
        "evidence_role": evidence_role,
        "asof_date": asof,
        "availability_snapshot_sha256": snapshot_sha,
        "panel_input_rows_sha256": payload["panel_input_rows_sha256"],
        "accepted_fact_input_rows_sha256": payload[
            "accepted_fact_input_rows_sha256"
        ],
        "input_count": len(expected_panel) + len(expected_facts),
        "panel_input_count": len(expected_panel),
        "accepted_fact_input_count": len(expected_facts),
        "input_identity_census_sha256": canonical_sha256(identity_census),
        "input_content_census_sha256": canonical_sha256(content_census),
        "input_value_census_sha256": canonical_sha256(value_census),
        "source_observation_mapping_sha256": canonical_sha256(mappings),
        "provider_dataset_pair_count": len(pair_payload),
        "provider_dataset_pairs_sha256": canonical_sha256(pair_payload),
        "source_max_information_at_utc": max_available.isoformat(),
        "status_effective_through_at_utc": cutoff.isoformat(),
        "status_asof_policy": TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
    }
    for field, expected in expected_claims.items():
        if attestation.get(field) != expected:
            raise ValueError(
                f"Transportation availability attestation changed field: {field}"
            )
    for field in (
        "input_count",
        "panel_input_count",
        "accepted_fact_input_count",
        "provider_dataset_pair_count",
    ):
        _strict_int(attestation.get(field), label=f"attested {field}")
    _strict_sha256(
        attestation.get("query_sha256"),
        label="Transportation availability query sha256",
    )
    if effective_through != cutoff or not cutoff <= generated <= exported:
        raise ValueError("Transportation availability attestation chronology is invalid")

    predecessor_pass = predecessor_availability_audit is None
    no_backdated_panel_append_pass = predecessor_availability_audit is None
    no_backdated_fact_append_pass = predecessor_availability_audit is None
    if predecessor_availability_audit is not None:
        predecessor = predecessor_availability_audit
        if not isinstance(predecessor, Mapping):
            raise ValueError("Transportation predecessor availability audit is invalid")
        if (
            predecessor.get("schema_version")
            != TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA
            or predecessor.get("source_attestation_schema")
            != TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
            or predecessor.get("evidence_role")
            not in {PROSPECTIVE_ROLE, ACTIVATION_BASELINE_ROLE}
            or predecessor.get("exact_panel_input_census_pass") is not True
            or predecessor.get("exact_accepted_fact_input_census_pass") is not True
            or predecessor.get("no_post_cutoff_score_inputs_pass") is not True
            or predecessor.get("no_backdated_panel_append_pass") is not True
            or predecessor.get("no_backdated_fact_append_pass") is not True
        ):
            raise ValueError(
                "Transportation predecessor availability audit identity changed"
            )
        prior_asof = _exact_date(
            predecessor.get("asof_date"),
            label="Transportation predecessor availability asof",
        )
        if prior_asof >= asof:
            raise ValueError("Transportation predecessor availability is not earlier")
        prior_cutoff = _utc(
            predecessor.get("signal_cutoff_at_utc"),
            label="Transportation predecessor availability cutoff",
        )
        if (
            prior_cutoff.date().isoformat() != prior_asof
            or prior_cutoff >= cutoff
        ):
            raise ValueError(
                "Transportation predecessor availability cutoff is not the prior asof"
            )
        prior_panel_count = _strict_int(
            predecessor.get("panel_input_row_count"),
            label="Transportation predecessor panel availability count",
            minimum=1,
        )
        prior_fact_count = _strict_int(
            predecessor.get("accepted_fact_input_row_count"),
            label="Transportation predecessor fact availability count",
        )
        if (
            prior_panel_count > len(panel_rows)
            or predecessor.get("full_panel_input_rows_sha256")
            != canonical_sha256(panel_rows[:prior_panel_count])
            or prior_fact_count > len(fact_rows)
            or predecessor.get("full_accepted_fact_input_rows_sha256")
            != canonical_sha256(fact_rows[:prior_fact_count])
        ):
            raise ValueError(
                "Transportation score-input availability predecessor prefix changed"
            )
        predecessor_label = (
            "baseline"
            if predecessor.get("evidence_role") == ACTIVATION_BASELINE_ROLE
            else "prior"
        )
        if any(timestamp <= prior_cutoff for timestamp in panel_times[prior_panel_count:]):
            raise ValueError(
                "Transportation newly appended panel input is backdated at/before "
                f"{predecessor_label} cutoff"
            )
        if any(timestamp <= prior_cutoff for timestamp in fact_times[prior_fact_count:]):
            raise ValueError(
                "Transportation newly appended fact input is backdated at/before "
                f"{predecessor_label} cutoff"
            )
        no_backdated_panel_append_pass = True
        no_backdated_fact_append_pass = True
        predecessor_pass = True

    panel_index = {
        str(row["input_identity_sha256"]): row for row in panel_rows
    }
    fact_index = {str(row["input_identity_sha256"]): row for row in fact_rows}
    return panel_index, fact_index, {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "source_attestation_schema": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "evidence_role": evidence_role,
        "asof_date": asof,
        "snapshot_sha256": snapshot_sha,
        "source_attestation_sha256": receipt_sha,
        "panel_input_row_count": len(panel_rows),
        "accepted_fact_input_row_count": len(fact_rows),
        "full_panel_input_rows_sha256": canonical_sha256(panel_rows),
        "full_accepted_fact_input_rows_sha256": canonical_sha256(fact_rows),
        "input_identity_census_sha256": canonical_sha256(identity_census),
        "input_content_census_sha256": canonical_sha256(content_census),
        "input_value_census_sha256": canonical_sha256(value_census),
        "source_observation_mapping_sha256": canonical_sha256(mappings),
        "provider_dataset_pairs_sha256": canonical_sha256(pair_payload),
        "source_observation_count": len(shared_records),
        "snapshot_generated_at_utc": generated.isoformat(),
        "source_attestation_exported_at_utc": exported.isoformat(),
        "signal_cutoff_at_utc": cutoff.isoformat(),
        "status_effective_through_at_utc": effective_through.isoformat(),
        "max_source_available_at_utc": max_available.isoformat(),
        "exact_panel_input_census_pass": True,
        "exact_accepted_fact_input_census_pass": True,
        "exact_input_content_and_value_pass": True,
        "exact_input_mapping_pass": True,
        "source_record_consistency_pass": True,
        "exact_official_close_cutoff_pass": True,
        "predecessor_availability_prefix_pass": predecessor_pass,
        "no_backdated_panel_append_pass": no_backdated_panel_append_pass,
        "no_backdated_fact_append_pass": no_backdated_fact_append_pass,
        "market_authority_attested_score_inputs_pass": True,
        "no_post_cutoff_score_inputs_pass": True,
        "production_activation_authorized": False,
    }


def validate_transport_score_input_availability_capture_chronology(
    availability_audit: Mapping[str, Any],
    *,
    trusted_capture_timing: Mapping[str, Any],
    captured_at_utc: Any,
    label: str,
) -> dict[str, Any]:
    """Bind a replayed Transportation input audit to signed capture timing."""

    if not isinstance(availability_audit, Mapping) or not isinstance(
        trusted_capture_timing, Mapping
    ):
        raise ValueError(f"{label} availability/capture timing audit is invalid")
    cutoff = _utc(
        trusted_capture_timing.get("signal_information_cutoff_at_utc"),
        label=f"{label} signed signal cutoff",
    )
    signed_max = _utc(
        trusted_capture_timing.get("source_max_information_at_utc"),
        label=f"{label} signed source max information time",
    )
    signed_generated = _utc(
        trusted_capture_timing.get("source_generated_at_utc"),
        label=f"{label} signed source generation time",
    )
    captured = _utc(captured_at_utc, label=f"{label} capture time")
    signed_captured = _utc(
        trusted_capture_timing.get("captured_at_utc"),
        label=f"{label} signed capture time",
    )
    entry = _utc(
        trusted_capture_timing.get("entry_execution_at_utc"),
        label=f"{label} entry time",
    )
    max_available = _utc(
        availability_audit.get("max_source_available_at_utc"),
        label=f"{label} availability max information time",
    )
    generated = _utc(
        availability_audit.get("snapshot_generated_at_utc"),
        label=f"{label} availability generation time",
    )
    exported = _utc(
        availability_audit.get("source_attestation_exported_at_utc"),
        label=f"{label} availability export time",
    )
    effective_through = _utc(
        availability_audit.get("status_effective_through_at_utc"),
        label=f"{label} availability effective-through time",
    )
    audit_cutoff = _utc(
        availability_audit.get("signal_cutoff_at_utc"),
        label=f"{label} availability audit cutoff",
    )
    if (
        availability_audit.get("exact_official_close_cutoff_pass") is not True
        or availability_audit.get("no_post_cutoff_score_inputs_pass") is not True
        or effective_through != cutoff
        or audit_cutoff != cutoff
        or captured != signed_captured
        or max_available > signed_max
        or generated > signed_generated
        or exported > signed_generated
        or not cutoff <= generated <= exported <= signed_generated <= captured < entry
    ):
        raise ValueError(
            f"{label} Transportation score-input availability exceeds signed capture timing"
        )
    return {
        "exact_official_close_availability_pass": True,
        "availability_within_signed_source_envelope_pass": True,
        "availability_export_before_capture_pass": True,
        "capture_before_entry_pass": True,
    }


__all__ = [
    "ACTIVATION_BASELINE_ROLE",
    "AVAILABILITY_ATTESTATION_FIELDS",
    "AVAILABILITY_ROW_FIELDS",
    "AVAILABILITY_TOP_LEVEL_FIELDS",
    "FACT_INPUT_KIND",
    "PANEL_INPUT_KIND",
    "TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA",
    "TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY",
    "TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA",
    "transport_fact_identity",
    "transport_fact_input_value_sha256",
    "transport_panel_input_identity",
    "transport_panel_input_value_sha256",
    "transport_score_input_availability_contract",
    "validate_transport_score_input_availability_capture_chronology",
    "validate_transport_score_input_availability_snapshot",
]
