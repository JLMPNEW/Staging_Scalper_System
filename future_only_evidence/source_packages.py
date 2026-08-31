"""Unsigned, create-only builders for canonical future-evidence source packages.

These builders only package point-in-time signal inputs.  They never create
receipts, timestamps, outcomes, evaluations, or production authorization.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.lifecycle_snapshot import (
    ACTIVE_SOURCE_MAX_AGE_HOURS,
    LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
    LIFECYCLE_STATUS_ASOF_POLICY,
    LIFECYCLE_ROW_FIELDS,
    TERMINAL_EVENT_TYPE_TO_REASON,
    validate_lifecycle_event_snapshot,
)
from future_only_evidence.canonical_trust import CanonicalTrustBundle
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.transport_score_input_availability import (
    ACTIVATION_BASELINE_ROLE,
    AVAILABILITY_ROW_FIELDS as TRANSPORT_AVAILABILITY_ROW_FIELDS,
    FACT_INPUT_KIND,
    PANEL_INPUT_KIND,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
    transport_fact_identity,
    transport_fact_input_value_sha256,
    transport_panel_input_identity,
    transport_panel_input_value_sha256,
)
from future_only_evidence.protocol import (
    canonical_sha256,
    file_sha256,
    immutable_write_json,
)
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from industrials.transportation.future_oos_activation_v6 import (
    GROUP_TICKERS,
    GROUP_WEIGHTS,
    LIFECYCLE_EVENT_SCHEMA_V6,
    validate_frozen_v8_policy,
)
from industrials.transportation.future_oos_capture_v6 import MEMBERSHIP_SCHEMA_V6
from industrials.transportation.future_oos_score_lineage_v1 import (
    ACCEPTED_FACTS_SCHEMA,
    BASELINE_EVIDENCE_ROLE,
    BASELINE_SCHEMA,
    GOVERNED_HORIZON_SESSIONS,
    PANEL_SCHEMA,
    STRUCTURAL_BASELINE_SOURCE_ROLES,
    STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES,
    validate_transport_replay_inputs_structure,
    validate_transport_score_replay_baseline_structure,
)


_TRANSPORT_PANEL_FIELDS = frozenset(
    {
        "asof_date",
        "ticker",
        "horizon_sessions",
        "calibration_cohort",
        "metric_values_json",
        "metric_status_json",
        "positioning_score",
        "rank_ready_flag",
        "calibration_eligible_flag",
        "source_score_sha256",
    }
)
_TRANSPORT_FACT_REQUIRED = frozenset(
    {
        "ticker",
        "metric_id",
        "value",
        "unit",
        "period_end",
        "filing_date",
        "accepted_at",
        "replay_status",
    }
)
_OUTCOME_TOKENS = (
    "forward_",
    "outcome",
    "realized",
    "benchmark_return",
    "security_return",
    "exit_date",
    "exit_price",
    "target_",
)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    raw = Path(path).expanduser().resolve().read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _read_csv(path: Path, *, label: str) -> tuple[list[dict[str, str]], list[str]]:
    raw = Path(path).expanduser().resolve().read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        rows = [dict(row) for row in reader]
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    fields = list(reader.fieldnames or [])
    if not fields:
        raise ValueError(f"{label} has no header")
    return rows, fields


def _canonical_float(text: Any, *, label: str) -> float:
    if type(text) is not str or not text or text.strip() != text:
        raise ValueError(f"{label} must be a canonical numeric CSV field")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


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


def _exact_sha256(value: Any, *, label: str) -> str:
    text = _canonical_text(value, label=label)
    if len(text) != 64 or text.lower() != text:
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be lowercase SHA-256 hex") from exc
    return text


def _write_result(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    written = immutable_write_json(path, payload)
    return {
        "path": str(written),
        "sha256": file_sha256(written),
        "bytes": written.stat().st_size,
    }


def build_lifecycle_event_snapshot(
    *,
    family: str,
    policy_id: str,
    raw_lifecycle_csv_path: Path,
    expected_tickers: Sequence[str],
    asof_date: str,
    signal_cutoff_at_utc: str,
    snapshot_generated_at_utc: str,
    query_sha256: str,
    output_path: Path,
    signing_request_output_path: Path,
) -> dict[str, Any]:
    """Build an unsigned lifecycle ledger and its external-signing request.

    The result is deliberately not capture-ready.  A pinned, independent
    market-data authority must verify the query and sign the resulting
    lifecycle source attestation before membership can be constructed.
    """

    schemas = {"transportation": LIFECYCLE_EVENT_SCHEMA_V6}
    if family not in schemas:
        raise ValueError("lifecycle package family is unsupported")
    if output_path.exists() or signing_request_output_path.exists():
        raise FileExistsError("lifecycle source-package artifacts are create-only")
    policy = _canonical_text(policy_id, label="lifecycle policy id")
    asof = _exact_date(asof_date, label="lifecycle asof")
    cutoff = _utc(signal_cutoff_at_utc, label="lifecycle signal cutoff")
    generated = _utc(
        snapshot_generated_at_utc, label="lifecycle snapshot generation time"
    )
    query_digest = _exact_sha256(query_sha256, label="lifecycle query sha256")
    ticker_list = list(expected_tickers)
    if (
        not ticker_list
        or len(set(ticker_list)) != len(ticker_list)
        or any(
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            for ticker in ticker_list
        )
    ):
        raise ValueError("expected lifecycle ticker census is not canonical")
    rows, fields = _read_csv(raw_lifecycle_csv_path, label="raw lifecycle ledger")
    if set(fields) != LIFECYCLE_ROW_FIELDS:
        raise ValueError("raw lifecycle ledger field census changed")
    normalized: list[dict[str, Any]] = []
    nullable = {
        "terminal_event_type",
        "terminal_event_effective_at_utc",
        "terminal_event_reason_code",
    }
    seen: set[str] = set()
    observation_ids: set[str] = set()
    provider_ids: set[str] = set()
    dataset_ids: set[str] = set()
    max_available: datetime | None = None
    for raw in rows:
        row = {
            field: (None if field in nullable and raw[field] == "" else raw[field])
            for field in fields
        }
        if row["asof_date"] != asof:
            raise ValueError("lifecycle row asof differs from requested asof")
        ticker = _canonical_text(row["ticker"], label="lifecycle ticker")
        if ticker.upper() != ticker or ticker in seen:
            raise ValueError("lifecycle tickers must be unique canonical uppercase")
        seen.add(ticker)
        available = _utc(
            row["source_available_at_utc"],
            label=f"{ticker} lifecycle source availability",
        )
        if available > cutoff or available > generated:
            raise ValueError(f"{ticker}: lifecycle source chronology is invalid")
        observation_id = _canonical_text(
            row["source_observation_id"],
            label=f"{ticker} lifecycle observation id",
        )
        if observation_id in observation_ids:
            raise ValueError("lifecycle observation ids must be unique")
        observation_ids.add(observation_id)
        _canonical_text(row["source_locator"], label=f"{ticker} lifecycle locator")
        _exact_sha256(
            row["source_record_sha256"],
            label=f"{ticker} lifecycle source record sha256",
        )
        provider_ids.add(
            _canonical_text(row["provider_id"], label=f"{ticker} provider id")
        )
        dataset_ids.add(
            _canonical_text(row["dataset_id"], label=f"{ticker} dataset id")
        )
        status = row["lifecycle_status_at_signal_cutoff"]
        if status == "active":
            if any(row[field] is not None for field in nullable):
                raise ValueError(f"{ticker}: active lifecycle row has terminal fields")
            if available < cutoff - timedelta(hours=ACTIVE_SOURCE_MAX_AGE_HOURS):
                raise ValueError(f"{ticker}: active lifecycle assertion is stale")
        elif status == "governed_terminal_event":
            event_type = row["terminal_event_type"]
            if event_type not in TERMINAL_EVENT_TYPE_TO_REASON:
                raise ValueError(f"{ticker}: terminal event type is outside policy")
            if (
                row["terminal_event_reason_code"]
                != TERMINAL_EVENT_TYPE_TO_REASON[event_type]
            ):
                raise ValueError(f"{ticker}: terminal event reason is inconsistent")
            effective = _utc(
                row["terminal_event_effective_at_utc"],
                label=f"{ticker} terminal event time",
            )
            if effective > available or effective > cutoff:
                raise ValueError(f"{ticker}: terminal event is post-cutoff knowledge")
        else:
            raise ValueError(f"{ticker}: lifecycle status is outside policy")
        max_available = (
            available if max_available is None else max(max_available, available)
        )
        normalized.append(row)
    normalized.sort(key=lambda row: str(row["ticker"]))
    if set(seen) != set(ticker_list) or len(seen) != len(ticker_list):
        raise ValueError("raw lifecycle ledger differs from frozen ticker census")
    if len(provider_ids) != 1 or len(dataset_ids) != 1 or max_available is None:
        raise ValueError("lifecycle ledger must use one provider and one dataset")
    payload = {
        "schema_version": schemas[family],
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": asof,
        "snapshot_generated_at_utc": snapshot_generated_at_utc,
        "rows": normalized,
        "rows_sha256": canonical_sha256(normalized),
    }
    payload_bytes = _json_bytes(payload)
    snapshot_digest = hashlib.sha256(payload_bytes).hexdigest()
    signing_claims = {
        "schema_version": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "family": family,
        "policy_id": policy,
        "asof_date": asof,
        "lifecycle_snapshot_sha256": snapshot_digest,
        "lifecycle_rows_sha256": payload["rows_sha256"],
        "ticker_count": len(seen),
        "ticker_census_sha256": canonical_sha256(sorted(seen)),
        "provider_id": next(iter(provider_ids)),
        "dataset_id": next(iter(dataset_ids)),
        "source_max_information_at_utc": max_available.isoformat(),
        "status_effective_through_at_utc": cutoff.isoformat(),
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
        "query_sha256": query_digest,
        "observation_ids_sha256": canonical_sha256(sorted(observation_ids)),
    }
    signing_request = {
        "schema_version": "future_lifecycle_source_attestation_signing_request_v1",
        "evidence_class": "prospective_future_only",
        "attestation_schema_version": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "unsigned_attestation_claims": signing_claims,
        "authority_must_add_fields": [
            "authority_id",
            "exported_at_utc",
            "signature_base64",
            "signed_payload_sha256",
        ],
        "required_authority_role": "pinned_market_data_export_ed25519_authority",
        "request_is_not_a_trusted_attestation": True,
        "capture_ready": False,
        "production_activation_authorized": False,
    }
    return {
        "schema_version": "future_only_lifecycle_source_package_audit_v1",
        "family": family,
        "artifact": _write_result(output_path, payload),
        "signing_request": _write_result(
            signing_request_output_path, signing_request
        ),
        "unsigned_row_validation_pass": True,
        "external_attestation_required": True,
        "capture_ready": False,
        "production_activation_authorized": False,
    }


def _transport_panel_payload(
    *, raw_panel_path: Path, asof_date: str
) -> dict[str, Any]:
    rows, fields = _read_csv(raw_panel_path, label="Transportation PIT scoring panel")
    if set(fields) != _TRANSPORT_PANEL_FIELDS:
        raise ValueError(
            "Transportation PIT panel must contain only the exact outcome-blind fields"
        )
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if raw["asof_date"] > asof_date:
            raise ValueError("Transportation PIT panel contains post-checkpoint rows")
        if raw["horizon_sessions"] != str(GOVERNED_HORIZON_SESSIONS):
            raise ValueError("Transportation PIT panel horizon is not canonical 63")
        if raw["rank_ready_flag"] not in {"0", "1"} or raw[
            "calibration_eligible_flag"
        ] not in {"0", "1"}:
            raise ValueError("Transportation PIT panel flags must be exact 0/1")
        try:
            values = json.loads(raw["metric_values_json"])
            statuses = json.loads(raw["metric_status_json"])
        except json.JSONDecodeError as exc:
            raise ValueError("Transportation PIT panel metric JSON is invalid") from exc
        if not isinstance(values, dict) or not isinstance(statuses, dict):
            raise ValueError("Transportation PIT panel metric JSON must be objects")
        normalized.append(
            {
                **raw,
                "horizon_sessions": GOVERNED_HORIZON_SESSIONS,
                "metric_values_json": json.dumps(
                    values, sort_keys=True, separators=(",", ":")
                ),
                "metric_status_json": json.dumps(
                    statuses, sort_keys=True, separators=(",", ":")
                ),
                "positioning_score": (
                    None
                    if raw["positioning_score"] == ""
                    else _canonical_float(
                        raw["positioning_score"], label="positioning score"
                    )
                ),
                "rank_ready_flag": int(raw["rank_ready_flag"]),
                "calibration_eligible_flag": int(
                    raw["calibration_eligible_flag"]
                ),
            }
        )
    normalized.sort(key=lambda row: (row["asof_date"], row["ticker"]))
    census: dict[str, list[str]] = {}
    for row in normalized:
        census.setdefault(str(row["asof_date"]), []).append(str(row["ticker"]))
    return {
        "schema_version": PANEL_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": asof_date,
        "governed_horizon_sessions": GOVERNED_HORIZON_SESSIONS,
        "rows": normalized,
        "rows_sha256": canonical_sha256(normalized),
        "date_ticker_census": census,
        "date_ticker_census_sha256": canonical_sha256(census),
    }


def _transport_facts_payload(
    *,
    raw_facts_path: Path,
    staleness_path: Path,
    asof_date: str,
    preserve_input_order: bool = False,
) -> dict[str, Any]:
    rows, fields = _read_csv(raw_facts_path, label="Transportation accepted facts")
    if not _TRANSPORT_FACT_REQUIRED <= set(fields):
        raise ValueError("Transportation accepted facts lack required fields")
    if any(any(token in field.casefold() for token in _OUTCOME_TOKENS) for field in fields):
        raise ValueError("Transportation accepted facts contain outcome fields")
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row: dict[str, Any] = {
            field: (None if value == "" else value) for field, value in raw.items()
        }
        row["value"] = _canonical_float(raw["value"], label="accepted fact value")
        normalized.append(row)
    if not preserve_input_order:
        normalized.sort(
            key=lambda row: (
                str(row.get("ticker")),
                str(row.get("metric_id")),
                str(row.get("period_end")),
                str(row.get("candidate_key") or row.get("evidence_key")),
            )
        )
    staleness = _read_json(staleness_path, label="Transportation staleness policy")
    if any(type(value) is not int or value <= 0 for value in staleness.values()):
        raise ValueError("Transportation staleness values must be positive integers")
    return {
        "schema_version": ACCEPTED_FACTS_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": asof_date,
        "rows": normalized,
        "rows_sha256": canonical_sha256(normalized),
        "staleness_days": staleness,
        "staleness_days_sha256": canonical_sha256(staleness),
    }


def _transport_fact_identity(row: Mapping[str, Any]) -> str:
    source_identity = row.get("candidate_key") or row.get("evidence_key")
    if type(source_identity) is not str or not source_identity:
        raise ValueError("Transportation accepted fact lacks source identity")
    return canonical_sha256(
        {
            "ticker": row.get("ticker"),
            "metric_id": row.get("metric_id"),
            "period_end": row.get("period_end"),
            "candidate_key": row.get("candidate_key"),
            "evidence_key": row.get("evidence_key"),
            "source_content_sha256": row.get("source_content_sha256"),
        }
    )


def _build_transport_availability_request(
    *,
    panel_rows: Sequence[Mapping[str, Any]],
    accepted_fact_rows: Sequence[Mapping[str, Any]],
    raw_source_availability_csv_path: Path,
    asof_date: str,
    signal_cutoff_at_utc: str,
    snapshot_generated_at_utc: str,
    policy_id: str,
    query_sha256: str,
    evidence_role: str,
    output_path: Path,
    signing_request_output_path: Path,
) -> dict[str, Any]:
    if output_path.exists() or signing_request_output_path.exists():
        raise FileExistsError(
            "Transportation availability-request artifacts are create-only"
        )
    asof = _exact_date(asof_date, label="Transportation availability asof")
    cutoff = _utc(
        signal_cutoff_at_utc,
        label="Transportation availability signal cutoff",
    )
    generated = _utc(
        snapshot_generated_at_utc,
        label="Transportation availability snapshot generation time",
    )
    if cutoff.date().isoformat() != asof or generated < cutoff:
        raise ValueError("Transportation availability generation/cutoff is invalid")
    if evidence_role not in {PROSPECTIVE_ROLE, ACTIVATION_BASELINE_ROLE}:
        raise ValueError("Transportation availability evidence role is unsupported")
    policy = _canonical_text(policy_id, label="Transportation policy id")
    query_digest = _exact_sha256(
        query_sha256,
        label="Transportation availability query sha256",
    )

    expected: list[dict[str, str]] = []
    for raw in panel_rows:
        row = dict(raw)
        expected.append(
            {
                "input_kind": PANEL_INPUT_KIND,
                "ticker": str(row["ticker"]),
                "input_identity_sha256": transport_panel_input_identity(row),
                "input_content_sha256": canonical_sha256(row),
                "input_value_sha256": transport_panel_input_value_sha256(row),
            }
        )
    for raw in accepted_fact_rows:
        row = dict(raw)
        expected.append(
            {
                "input_kind": FACT_INPUT_KIND,
                "ticker": str(row["ticker"]),
                "input_identity_sha256": transport_fact_identity(row),
                "input_content_sha256": canonical_sha256(row),
                "input_value_sha256": transport_fact_input_value_sha256(row),
            }
        )
    if not panel_rows:
        raise ValueError("Transportation availability requires scoring-panel inputs")

    raw_rows, fields = _read_csv(
        raw_source_availability_csv_path,
        label="Transportation score-input source availability",
    )
    if set(fields) != TRANSPORT_AVAILABILITY_ROW_FIELDS or len(raw_rows) != len(
        expected
    ):
        raise ValueError("Transportation availability crosswalk census changed")
    normalized: list[dict[str, Any]] = []
    source_records: dict[str, tuple[str, ...]] = {}
    mappings: list[dict[str, Any]] = []
    provider_pairs: set[tuple[str, str]] = set()
    max_available: datetime | None = None
    for position, (raw, expected_row) in enumerate(zip(raw_rows, expected)):
        if raw["source_required_flag"] != "1":
            raise ValueError(
                "Transportation score-driving input requires one source record"
            )
        for field in (
            "input_kind",
            "ticker",
            "input_identity_sha256",
            "input_content_sha256",
            "input_value_sha256",
        ):
            if raw[field] != expected_row[field]:
                raise ValueError(
                    "Transportation availability differs from input at "
                    f"position {position}"
                )
        if raw["availability_status"] != "available":
            raise ValueError(
                "Transportation score-driving input is not attested available"
            )
        ticker = _canonical_text(raw["ticker"], label="Transportation ticker")
        if ticker.upper() != ticker:
            raise ValueError("Transportation availability ticker is not canonical")
        source_id = _canonical_text(
            raw["source_id"], label=f"{ticker} source id"
        )
        available = _utc(
            raw["source_available_at_utc"],
            label=f"{ticker} source availability",
        )
        if available > cutoff or available > generated:
            raise ValueError("Transportation score input is post-cutoff")
        observation_id = _canonical_text(
            raw["source_observation_id"],
            label=f"{ticker} source observation id",
        )
        locator = _canonical_text(
            raw["source_locator"], label=f"{ticker} source locator"
        )
        record_sha = _exact_sha256(
            raw["source_record_sha256"],
            label=f"{ticker} source record sha256",
        )
        provider = _canonical_text(
            raw["provider_id"], label=f"{ticker} provider id"
        )
        dataset = _canonical_text(
            raw["dataset_id"], label=f"{ticker} dataset id"
        )
        provenance = (
            ticker,
            provider,
            dataset,
            source_id,
            available.isoformat(),
            locator,
            record_sha,
        )
        prior = source_records.setdefault(observation_id, provenance)
        if prior != provenance:
            raise ValueError(
                "Transportation reused source observation has inconsistent provenance"
            )
        row: dict[str, Any] = {
            **expected_row,
            "availability_status": "available",
            "source_required_flag": 1,
            "source_id": source_id,
            "source_available_at_utc": available.isoformat(),
            "source_observation_id": observation_id,
            "source_locator": locator,
            "source_record_sha256": record_sha,
            "provider_id": provider,
            "dataset_id": dataset,
        }
        normalized.append(row)
        mappings.append(
            {
                "input_kind": row["input_kind"],
                "ticker": ticker,
                "input_identity_sha256": row["input_identity_sha256"],
                "input_content_sha256": row["input_content_sha256"],
                "input_value_sha256": row["input_value_sha256"],
                "source_observation_id": observation_id,
                "source_record_sha256": record_sha,
                "source_available_at_utc": available.isoformat(),
                "source_locator": locator,
                "source_id": source_id,
                "provider_id": provider,
                "dataset_id": dataset,
            }
        )
        provider_pairs.add((provider, dataset))
        max_available = (
            available if max_available is None else max(max_available, available)
        )
    if max_available is None:
        raise ValueError("Transportation availability lacks source information")
    panel_count = len(panel_rows)
    panel_availability = normalized[:panel_count]
    fact_availability = normalized[panel_count:]
    payload = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "evidence_role": evidence_role,
        "asof_date": asof,
        "snapshot_generated_at_utc": generated.isoformat(),
        "panel_input_rows": panel_availability,
        "panel_input_rows_sha256": canonical_sha256(panel_availability),
        "accepted_fact_input_rows": fact_availability,
        "accepted_fact_input_rows_sha256": canonical_sha256(
            fact_availability
        ),
    }
    payload_bytes = _json_bytes(payload)
    identity_census = {
        "panel": [row["input_identity_sha256"] for row in panel_availability],
        "accepted_facts": [
            row["input_identity_sha256"] for row in fact_availability
        ],
    }
    content_census = {
        "panel": [row["input_content_sha256"] for row in panel_availability],
        "accepted_facts": [
            row["input_content_sha256"] for row in fact_availability
        ],
    }
    value_census = {
        "panel": [row["input_value_sha256"] for row in panel_availability],
        "accepted_facts": [
            row["input_value_sha256"] for row in fact_availability
        ],
    }
    pair_payload = [
        {"provider_id": provider, "dataset_id": dataset}
        for provider, dataset in sorted(provider_pairs)
    ]
    claims = {
        "schema_version": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "family": "transportation",
        "policy_id": policy,
        "evidence_role": evidence_role,
        "asof_date": asof,
        "availability_snapshot_sha256": hashlib.sha256(
            payload_bytes
        ).hexdigest(),
        "panel_input_rows_sha256": payload["panel_input_rows_sha256"],
        "accepted_fact_input_rows_sha256": payload[
            "accepted_fact_input_rows_sha256"
        ],
        "input_count": len(normalized),
        "panel_input_count": panel_count,
        "accepted_fact_input_count": len(fact_availability),
        "input_identity_census_sha256": canonical_sha256(identity_census),
        "input_content_census_sha256": canonical_sha256(content_census),
        "input_value_census_sha256": canonical_sha256(value_census),
        "source_observation_mapping_sha256": canonical_sha256(mappings),
        "provider_dataset_pair_count": len(pair_payload),
        "provider_dataset_pairs_sha256": canonical_sha256(pair_payload),
        "source_max_information_at_utc": max_available.isoformat(),
        "status_effective_through_at_utc": cutoff.isoformat(),
        "status_asof_policy": TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
        "query_sha256": query_digest,
    }
    request = {
        "schema_version": (
            "transportation_future_score_input_availability_"
            "attestation_signing_request_v1"
        ),
        "evidence_class": "prospective_future_only",
        "attestation_schema_version": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "unsigned_attestation_claims": claims,
        "authority_must_add_fields": [
            "authority_id",
            "exported_at_utc",
            "signature_base64",
            "signed_payload_sha256",
        ],
        "required_authority_role": "pinned_market_data_export_ed25519_authority",
        "request_is_not_a_trusted_attestation": True,
        "capture_ready": False,
        "production_activation_authorized": False,
    }
    return {
        "availability_artifact": _write_result(output_path, payload),
        "availability_signing_request": _write_result(
            signing_request_output_path,
            request,
        ),
        "input_count": len(normalized),
        "source_observation_count": len(source_records),
        "external_attestation_required": True,
        "capture_ready": False,
        "production_activation_authorized": False,
    }


def build_transport_score_replay_baseline(
    *,
    baseline_cutoff_at_utc: str,
    activation_registered_at_utc: str,
    raw_panel_path: Path,
    raw_accepted_facts_path: Path,
    staleness_path: Path,
    v8_policy_path: Path,
    raw_source_availability_csv_path: Path,
    snapshot_generated_at_utc: str,
    policy_id: str,
    query_sha256: str,
    output_path: Path,
    availability_output_path: Path,
    availability_signing_request_output_path: Path,
) -> dict[str, Any]:
    """Freeze baseline inputs and request independent availability signing."""

    if (
        output_path.exists()
        or availability_output_path.exists()
        or availability_signing_request_output_path.exists()
    ):
        raise FileExistsError(
            "Transportation baseline source-package artifacts are create-only"
        )
    baseline_cutoff = _utc(
        baseline_cutoff_at_utc,
        label="Transportation replay baseline cutoff",
    )
    activation_registered = _utc(
        activation_registered_at_utc,
        label="Transportation planned activation registration",
    )
    if baseline_cutoff >= activation_registered:
        raise ValueError("Transportation baseline must predate activation registration")
    baseline_asof = baseline_cutoff.date().isoformat()
    panel = _transport_panel_payload(
        raw_panel_path=raw_panel_path,
        asof_date=baseline_asof,
    )
    facts = _transport_facts_payload(
        raw_facts_path=raw_accepted_facts_path,
        staleness_path=staleness_path,
        asof_date=baseline_asof,
    )
    source_hashes: dict[str, str] = {}
    for row in panel["rows"]:
        score_date = str(row["asof_date"])
        source_hash = _exact_sha256(
            row["source_score_sha256"],
            label=f"{score_date} source score sha256",
        )
        prior = source_hashes.setdefault(score_date, source_hash)
        if prior != source_hash:
            raise ValueError(
                "Transportation baseline mixes source-score identities within a date"
            )
    identities = [_transport_fact_identity(row) for row in facts["rows"]]
    if len(identities) != len(set(identities)):
        raise ValueError("Transportation baseline contains duplicate accepted facts")
    payload = {
        "schema_version": BASELINE_SCHEMA,
        "evidence_role": BASELINE_EVIDENCE_ROLE,
        "baseline_cutoff_at_utc": baseline_cutoff.isoformat(),
        "panel_rows": panel["rows"],
        "panel_rows_sha256": canonical_sha256(panel["rows"]),
        "date_ticker_census": panel["date_ticker_census"],
        "date_ticker_census_sha256": canonical_sha256(
            panel["date_ticker_census"]
        ),
        "source_score_file_sha256_by_date": dict(sorted(source_hashes.items())),
        "source_score_file_sha256_by_date_sha256": canonical_sha256(
            dict(sorted(source_hashes.items()))
        ),
        "accepted_fact_rows": facts["rows"],
        "accepted_fact_rows_sha256": canonical_sha256(facts["rows"]),
        "accepted_fact_identity_census_sha256": canonical_sha256(identities),
        "staleness_days": facts["staleness_days"],
        "staleness_days_sha256": canonical_sha256(facts["staleness_days"]),
    }
    baseline_bytes = _json_bytes(payload)
    policy_bytes = Path(v8_policy_path).expanduser().resolve().read_bytes()
    policy_audit = validate_frozen_v8_policy(
        v8_policy_path,
        policy_snapshot_bytes=policy_bytes,
    )
    structural_snapshots = {
        "score_replay_baseline": baseline_bytes,
        "v8_policy": policy_bytes,
    }
    if set(structural_snapshots) != STRUCTURAL_BASELINE_SOURCE_ROLES:
        raise ValueError("Transportation baseline structural role census changed")
    structural_audit = validate_transport_score_replay_baseline_structure(
        baseline_path=output_path,
        v8_policy_path=v8_policy_path,
        expected_baseline_cutoff_at_utc=baseline_cutoff.isoformat(),
        expected_sha256={
            role: hashlib.sha256(content).hexdigest()
            for role, content in structural_snapshots.items()
        },
        source_snapshot_bytes=structural_snapshots,
    )
    availability = _build_transport_availability_request(
        panel_rows=panel["rows"],
        accepted_fact_rows=facts["rows"],
        raw_source_availability_csv_path=raw_source_availability_csv_path,
        asof_date=baseline_asof,
        signal_cutoff_at_utc=baseline_cutoff.isoformat(),
        snapshot_generated_at_utc=snapshot_generated_at_utc,
        policy_id=policy_id,
        query_sha256=query_sha256,
        evidence_role=ACTIVATION_BASELINE_ROLE,
        output_path=availability_output_path,
        signing_request_output_path=(
            availability_signing_request_output_path
        ),
    )
    return {
        "schema_version": "transportation_future_replay_baseline_package_audit_v2",
        "artifact": _write_result(output_path, payload),
        "v8_policy_audit": policy_audit,
        "baseline_structure_audit": structural_audit,
        "score_input_availability_request": availability,
        "baseline_registered_at_upper_bound": activation_registered.isoformat(),
        "signed_baseline_validation_pending": True,
        "external_attestation_required": True,
        "capture_ready": False,
        "historical_outcome_or_revealed_input_accepted": False,
        "production_activation_authorized": False,
    }


def build_transport_replay_inputs(
    *,
    asof_date: str,
    raw_panel_path: Path,
    raw_accepted_facts_path: Path,
    staleness_path: Path,
    canonical_score_path: Path,
    score_replay_baseline_path: Path,
    v8_policy_path: Path,
    signal_cutoff_at_utc: str,
    scheduled_append_asof_dates: Sequence[str],
    raw_source_availability_csv_path: Path,
    snapshot_generated_at_utc: str,
    policy_id: str,
    query_sha256: str,
    panel_output_path: Path,
    accepted_facts_output_path: Path,
    availability_output_path: Path,
    availability_signing_request_output_path: Path,
    predecessor_replay_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build structurally valid inputs and request independent signing.

    Canonical score replay is deliberately deferred until the independent
    availability authority has signed the exact panel/fact input census.
    """

    if any(
        path.exists()
        for path in (
            panel_output_path,
            accepted_facts_output_path,
            availability_output_path,
            availability_signing_request_output_path,
        )
    ):
        raise FileExistsError("Transportation replay-input artifacts are create-only")
    asof = _exact_date(asof_date, label="Transportation replay asof")
    cutoff = _utc(
        signal_cutoff_at_utc,
        label="Transportation replay signal cutoff",
    )
    if cutoff.date().isoformat() != asof:
        raise ValueError("Transportation replay cutoff date differs from asof")
    panel = _transport_panel_payload(raw_panel_path=raw_panel_path, asof_date=asof_date)
    facts = _transport_facts_payload(
        raw_facts_path=raw_accepted_facts_path,
        staleness_path=staleness_path,
        asof_date=asof_date,
        preserve_input_order=True,
    )
    structural_snapshots = {
        "scoring_panel": _json_bytes(panel),
        "accepted_facts": _json_bytes(facts),
        "score_replay_baseline": (
            Path(score_replay_baseline_path).expanduser().resolve().read_bytes()
        ),
        "v8_policy": Path(v8_policy_path).expanduser().resolve().read_bytes(),
    }
    if set(structural_snapshots) != STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES:
        raise ValueError("Transportation replay structural role census changed")
    try:
        baseline_payload = json.loads(
            structural_snapshots["score_replay_baseline"].decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Transportation replay baseline is invalid UTF-8 JSON") from exc
    if not isinstance(baseline_payload, dict):
        raise ValueError("Transportation replay baseline must be a JSON object")
    baseline_cutoff = _utc(
        baseline_payload.get("baseline_cutoff_at_utc"),
        label="Transportation replay baseline cutoff",
    )
    structural_audit = validate_transport_replay_inputs_structure(
        asof_date=asof,
        signal_cutoff_at_utc=cutoff.isoformat(),
        scheduled_append_asof_dates=scheduled_append_asof_dates,
        scoring_panel_path=panel_output_path,
        accepted_facts_path=accepted_facts_output_path,
        score_replay_baseline_path=score_replay_baseline_path,
        v8_policy_path=v8_policy_path,
        expected_baseline_cutoff_at_utc=baseline_cutoff.isoformat(),
        expected_sha256={
            role: hashlib.sha256(content).hexdigest()
            for role, content in structural_snapshots.items()
        },
        predecessor_replay_audit=predecessor_replay_audit,
        source_snapshot_bytes=structural_snapshots,
    )
    score_bytes = Path(canonical_score_path).expanduser().resolve().read_bytes()
    availability = _build_transport_availability_request(
        panel_rows=panel["rows"],
        accepted_fact_rows=facts["rows"],
        raw_source_availability_csv_path=raw_source_availability_csv_path,
        asof_date=asof,
        signal_cutoff_at_utc=cutoff.isoformat(),
        snapshot_generated_at_utc=snapshot_generated_at_utc,
        policy_id=policy_id,
        query_sha256=query_sha256,
        evidence_role=PROSPECTIVE_ROLE,
        output_path=availability_output_path,
        signing_request_output_path=availability_signing_request_output_path,
    )
    return {
        "schema_version": "transportation_future_replay_source_package_audit_v2",
        "asof_date": asof,
        "scoring_panel": _write_result(panel_output_path, panel),
        "accepted_facts": _write_result(accepted_facts_output_path, facts),
        "canonical_score_source_sha256": hashlib.sha256(score_bytes).hexdigest(),
        "replay_input_structure_audit": structural_audit,
        "score_input_availability_request": availability,
        "canonical_score_replay_validated": False,
        "score_replay_pending_signed_availability": True,
        "external_attestation_required": True,
        "capture_ready": False,
        "historical_or_revealed_input_accepted": False,
        "production_activation_authorized": False,
    }


def build_membership_snapshot(
    *,
    family: str,
    asof_date: str,
    lifecycle_snapshot_path: Path,
    lifecycle_attestation_path: Path,
    expected_lifecycle_attestation_sha256: str,
    signal_cutoff_at_utc: str,
    policy_id: str,
    trust_bundle: CanonicalTrustBundle,
    score_replay_audit: Mapping[str, Any],
    ticker_scope: Mapping[str, tuple[str, str]],
    output_path: Path,
) -> dict[str, Any]:
    """Reconcile model-data eligibility with an independent lifecycle ledger."""

    schemas = {"transportation": (LIFECYCLE_EVENT_SCHEMA_V6, MEMBERSHIP_SCHEMA_V6)}
    if family not in schemas:
        raise ValueError("membership package family is unsupported")
    lifecycle_schema, membership_schema = schemas[family]
    tickers = sorted(ticker_scope)
    lifecycle, lifecycle_audit = validate_lifecycle_event_snapshot(
        lifecycle_snapshot_path,
        expected_schema_version=lifecycle_schema,
        asof_date=asof_date,
        expected_tickers=tickers,
        signal_cutoff_at_utc=signal_cutoff_at_utc,
        family=family,
        policy_id=policy_id,
        attestation_path=lifecycle_attestation_path,
        expected_attestation_sha256=expected_lifecycle_attestation_sha256,
        bundle=trust_bundle,
    )
    eligibility = score_replay_audit.get("model_data_eligibility_by_ticker")
    if not isinstance(eligibility, dict) or set(eligibility) != set(tickers):
        raise ValueError("score replay eligibility differs from membership ticker scope")
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        sleeve, group = ticker_scope[ticker]
        source = lifecycle[ticker]
        lifecycle_flag = int(source["lifecycle_status_at_signal_cutoff"] == "active")
        model = eligibility[ticker]
        model_flag = model["model_data_eligible_flag"]
        reasons = list(model["model_data_exclusion_reason_codes"])
        if lifecycle_flag == 0:
            reasons.append("lifecycle_governed_terminal_event")
        reasons = sorted(set(reasons))
        common = {
            "asof_date": asof_date,
            "ticker": ticker,
            "group_id": group,
            "lifecycle_status_at_signal_cutoff": source[
                "lifecycle_status_at_signal_cutoff"
            ],
            "lifecycle_eligible_flag": lifecycle_flag,
            "model_data_eligible_flag": model_flag,
            "model_data_exclusion_reason_codes": model[
                "model_data_exclusion_reason_codes"
            ],
            "final_signal_eligible_flag": lifecycle_flag & model_flag,
            "final_signal_exclusion_reason_codes": reasons,
        }
        common["sleeve_id"] = sleeve
        rows.append(common)
    payload = {
        "schema_version": membership_schema,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": asof_date,
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }
    return {
        "schema_version": "future_only_membership_source_package_audit_v1",
        "family": family,
        "artifact": _write_result(output_path, payload),
        "lifecycle_audit": lifecycle_audit,
        "eligibility_policy": "lifecycle_intersect_deterministic_model_data_v1",
        "production_activation_authorized": False,
    }


def transportation_ticker_scope() -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for sleeve, groups in GROUP_WEIGHTS.items():
        for group in groups:
            for ticker in GROUP_TICKERS[group]:
                result[ticker] = (sleeve, group)
    return result


__all__ = [
    "build_lifecycle_event_snapshot",
    "build_membership_snapshot",
    "build_transport_score_replay_baseline",
    "build_transport_replay_inputs",
    "transportation_ticker_scope",
]
