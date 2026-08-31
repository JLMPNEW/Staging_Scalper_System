"""Canonical Consumer capture derived only from the bound rank snapshot."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import (
    PROSPECTIVE_ROLE,
    build_strict_capture,
)

from .future_oos_capture_v2 import REQUIRED_CAPTURE_ROLES_V2, REQUIRED_FRESHNESS_GATES
from .future_oos_plan_v3 import REQUIRED_PLAN_ROLES_V3
from .future_oos_plan_v4 import validate_registered_plan_v4


RANK_SNAPSHOT_SCHEMA = "consumer_defensive_future_rank_snapshot_v1"
MEMBERSHIP_SCHEMA = "consumer_defensive_future_membership_snapshot_v1"
SOURCE_MANIFEST_SCHEMA = "consumer_defensive_future_source_manifest_v2"
SOURCE_GENERATION_STATE = "outcome_blind_frozen_before_entry"


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


def derive_rank_signals(
    rank_path: Path,
    *,
    asof_date: str,
    rank_snapshot_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    expected_asof = str(asof_date)
    try:
        parsed_asof = date.fromisoformat(expected_asof)
    except ValueError as exc:
        raise ValueError("Consumer capture asof must be exact YYYY-MM-DD") from exc
    if parsed_asof.isoformat() != expected_asof:
        raise ValueError("Consumer capture asof must be exact YYYY-MM-DD")
    payload = _json(
        rank_path,
        label="Consumer future rank snapshot",
        snapshot_bytes=rank_snapshot_bytes,
    )
    if payload.get("schema_version") != RANK_SNAPSHOT_SCHEMA:
        raise ValueError("unsupported Consumer prospective rank snapshot")
    if payload.get("evidence_role") != PROSPECTIVE_ROLE:
        raise ValueError("Consumer rank snapshot is not prospective future-only capture input")
    if str(payload.get("asof_date") or "") != expected_asof:
        raise ValueError("Consumer rank snapshot asof mismatch")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Consumer rank snapshot has no rows")
    if payload.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("Consumer rank snapshot row hash mismatch")
    required = {
        "asof_date",
        "ticker",
        "sleeve_id",
        "group_id",
        "score",
        "rank",
        "ranking_mode",
        "eligible_flag",
        "selected_top_flag",
        "selected_bottom_flag",
    }
    by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for raw in rows:
        if required - set(raw):
            raise ValueError(f"Consumer rank row missing fields={sorted(required - set(raw))}")
        row = dict(raw)
        ticker = str(row["ticker"]).strip().upper()
        if not ticker or ticker in seen:
            raise ValueError("Consumer rank tickers must be globally unique")
        seen.add(ticker)
        if str(row["asof_date"]) != expected_asof:
            raise ValueError("Consumer rank row asof mismatch")
        if row["ranking_mode"] != "ranked":
            raise ValueError("Consumer cohort future evidence requires ranked rows")
        if isinstance(row["score"], bool):
            raise ValueError("Consumer rank score must not be boolean")
        score = float(row["score"])
        if not math.isfinite(score):
            raise ValueError("Consumer rank score is not finite")
        for field in (
            "rank",
            "eligible_flag",
            "selected_top_flag",
            "selected_bottom_flag",
        ):
            if type(row[field]) is not int:
                raise ValueError(f"Consumer rank {field} must be a canonical integer")
        if row["rank"] < 1 or any(
            row[field] not in (0, 1)
            for field in (
                "eligible_flag",
                "selected_top_flag",
                "selected_bottom_flag",
            )
        ):
            raise ValueError("Consumer rank/eligibility values are invalid")
        row.update(ticker=ticker, score=score)
        by_scope.setdefault((str(row["sleeve_id"]), str(row["group_id"])), []).append(row)
    signals: list[dict[str, Any]] = []
    for (sleeve, group), scope_rows in sorted(by_scope.items()):
        ordered = sorted(scope_rows, key=lambda row: (-float(row["score"]), row["ticker"]))
        if [row["rank"] for row in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError(f"{sleeve}/{group}: rank order does not match score/ticker order")
        eligible = [row for row in ordered if row["eligible_flag"] == 1]
        if len(eligible) < 2:
            raise ValueError(f"{sleeve}/{group}: insufficient eligible rank spread")
        count = max(1, math.ceil(0.20 * len(eligible)))
        top = {row["ticker"] for row in eligible[:count]}
        bottom = {row["ticker"] for row in eligible[-count:]}
        for row in ordered:
            expected_top = int(row["ticker"] in top)
            expected_bottom = int(row["ticker"] in bottom)
            if row["selected_top_flag"] != expected_top:
                raise ValueError(f"{row['ticker']}: top selection differs from frozen rank rule")
            if row["selected_bottom_flag"] != expected_bottom:
                raise ValueError(f"{row['ticker']}: bottom selection differs from frozen rank rule")
            signals.append(
                {
                    "asof_date": expected_asof,
                    "ticker": row["ticker"],
                    "sleeve_id": sleeve,
                    "group_id": group,
                    "score": float(row["score"]),
                    "rank": row["rank"],
                    "ranking_mode": "ranked",
                    "eligible_flag": row["eligible_flag"],
                    "predictive_eligible_flag": row["eligible_flag"],
                    "selected_top_flag": expected_top,
                    "selected_bottom_flag": expected_bottom,
                    "rank_source_row_sha256": canonical_sha256(row),
                }
            )
    return signals


def validate_capture_sources_v4(
    *,
    asof_date: str,
    signals: list[dict[str, Any]],
    source_paths: Mapping[str, Path],
) -> dict[str, Any]:
    manifest = _json(source_paths["source_manifest"], label="Consumer prospective source manifest")
    if manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise ValueError("unsupported Consumer prospective source manifest")
    if manifest.get("evidence_role") != PROSPECTIVE_ROLE:
        raise ValueError("Consumer source manifest role must be exact prospective capture")
    if manifest.get("source_generation_state") != SOURCE_GENERATION_STATE:
        raise ValueError("Consumer source manifest is not outcome-blind frozen state")
    if str(manifest.get("asof_date") or "")[:10] != str(asof_date)[:10]:
        raise ValueError("Consumer source manifest asof mismatch")
    for field in ("historical_results_can_authorize_production", "production_activation_authorized"):
        if manifest.get(field) is not False:
            raise ValueError(f"Consumer source manifest fail-closed field changed: {field}")
    freshness = manifest.get("freshness_gates")
    if not isinstance(freshness, dict) or set(freshness) != REQUIRED_FRESHNESS_GATES:
        raise ValueError("Consumer source freshness gate census changed")
    if any(freshness[field] is not True for field in REQUIRED_FRESHNESS_GATES):
        raise ValueError("Consumer source freshness gate failed")
    bound_roles = REQUIRED_CAPTURE_ROLES_V2 - {"source_manifest"}
    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict) or set(hashes) != bound_roles:
        raise ValueError("Consumer source manifest artifact roles are incomplete")
    for role in bound_roles:
        if hashes[role] != file_sha256(source_paths[role]):
            raise ValueError(f"Consumer source manifest hash mismatch: {role}")
    membership = _json(source_paths["membership_snapshot"], label="Consumer membership snapshot")
    if membership.get("schema_version") != MEMBERSHIP_SCHEMA:
        raise ValueError("unsupported Consumer prospective membership snapshot")
    if str(membership.get("asof_date") or "")[:10] != str(asof_date)[:10]:
        raise ValueError("Consumer membership snapshot asof mismatch")
    rows = membership.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Consumer membership rows are invalid")
    membership_index: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in membership_index:
            raise ValueError("Consumer membership has blank/duplicate tickers")
        membership_index[ticker] = dict(row)
    eligible = {row["ticker"]: row for row in signals if int(row["eligible_flag"]) == 1}
    if set(membership_index) != set(eligible):
        raise ValueError("Consumer membership census differs from bound eligible ranks")
    for ticker, signal in eligible.items():
        row = membership_index[ticker]
        if (
            str(row.get("cohort_id")) != signal["sleeve_id"]
            or str(row.get("group_id")) != signal["group_id"]
            or str(row.get("eligible_at_entry_flag")) != "1"
            or str(row.get("terminal_event_status"))
            not in {"none", "pending_governed_disposition"}
        ):
            raise ValueError(f"{ticker}: Consumer membership identity/lifecycle mismatch")
    return {
        "source_manifest_sha256": file_sha256(source_paths["source_manifest"]),
        "rank_snapshot_sha256": file_sha256(source_paths["rank_snapshot"]),
        "membership_snapshot_sha256": file_sha256(source_paths["membership_snapshot"]),
        "eligible_ticker_census_sha256": canonical_sha256(sorted(eligible)),
        "eligible_ticker_count": len(eligible),
        "rank_rows_derived_not_user_supplied": True,
        "exact_prospective_role_pass": True,
    }


def capture_signal(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    trusted_public_key_path: Path,
    authority_registry_path: Path,
    asof_date: str,
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    capture_receipt_path: Path,
    expected_capture_receipt_sha256: str,
) -> dict[str, Any]:
    plan, contract, authority, plan_audit = validate_registered_plan_v4(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        trusted_public_key_path=trusted_public_key_path,
        authority_registry_path=authority_registry_path,
    )
    if set(plan_source_paths) != REQUIRED_PLAN_ROLES_V3:
        raise ValueError("Consumer plan source roles changed")
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V2:
        raise ValueError("Consumer capture source roles changed")
    for role in REQUIRED_PLAN_ROLES_V3:
        if file_sha256(capture_source_paths[role]) != file_sha256(plan_source_paths[role]):
            raise ValueError(f"Consumer capture changed registered source: {role}")
    signals = derive_rank_signals(capture_source_paths["rank_snapshot"], asof_date=asof_date)
    source_audit = validate_capture_sources_v4(
        asof_date=asof_date,
        signals=signals,
        source_paths=capture_source_paths,
    )
    return build_strict_capture(
        contract=contract,
        asof_date=asof_date,
        signal_rows=signals,
        source_paths=capture_source_paths,
        expected_source_sha256=expected_capture_source_sha256,
        required_source_roles=REQUIRED_CAPTURE_ROLES_V2,
        trading_calendar_path=capture_source_paths["trading_calendar"],
        capture_receipt_path=capture_receipt_path,
        expected_capture_receipt_sha256=expected_capture_receipt_sha256,
        authority=authority,
        domain_fields={
            "domain_schema_version": "consumer_defensive_future_only_signal_capture_v4",
            "registered_plan_sha256": file_sha256(plan_path),
            "registration_receipt_sha256": file_sha256(registration_receipt_path),
            "baseline_state": plan["baseline_state"],
            "plan_integrity_audit": plan_audit,
            "source_semantics_audit": source_audit,
            "prospective_membership_tracking_required": True,
            "terminal_event_tracking_required": True,
        },
    )


__all__ = [
    "MEMBERSHIP_SCHEMA",
    "RANK_SNAPSHOT_SCHEMA",
    "SOURCE_MANIFEST_SCHEMA",
    "capture_signal",
    "derive_rank_signals",
    "validate_capture_sources_v4",
]
