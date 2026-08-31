"""Governing v7 future-only protocol for canonical Transportation v8 signals.

The historical script-42 calibration remains diagnostic.  This module starts
the production-evidence clock only from a contemporaneously captured canonical
v8 score/rank dated after the v7 design freeze and first eligible signal date.
Surface freight and tankers receive independent verdicts; tanker coverage is
an independent hard gate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.protocol import (
    FutureEvidencePolicy,
    TrustedReceiptVerifier,
    build_capture_payload,
    canonical_sha256,
    evaluate_future_evidence,
    file_sha256,
)


SCHEMA_VERSION = "transportation_v7_future_oos_protocol_v1"
POLICY_ID = "transportation_v7_governing_future_gate_v1"
POLICY_EFFECTIVE_FROM = date(2026, 8, 21)
FIRST_SIGNAL_DATE = date(2026, 8, 24)
REQUIRED_CAPTURE_ROLES = frozenset(
    {
        "canonical_v8_score",
        "canonical_v8_rank",
        "membership_snapshot",
        "source_manifest",
        "v8_policy",
        "v7_research_decision",
    }
)
TRANSPORT_POLICY = FutureEvidencePolicy(
    family="transportation",
    policy_id=POLICY_ID,
    effective_from=POLICY_EFFECTIVE_FROM,
    first_signal_date=FIRST_SIGNAL_DATE,
    horizons=(21, 63),
    minimum_counts={21: 12, 63: 4},
    minimum_ic=0.0,
    minimum_top_minus_cohort=0.0,
    minimum_top_minus_bottom=0.0,
    minimum_hit_rate=0.55,
    transaction_cost_bps=20.0,
    minimum_cross_sections={
        "north_american_surface_freight_and_logistics_v5": 20,
        "oil_tanker_operators_v5": 8,
        "rail_networks": 4,
        "ltl_carriers": 4,
        "truckload_intermodal": 6,
        "asset_light_logistics": 4,
        "oil_tankers": 8,
    },
    require_group_pass=True,
    top_minus_bottom_basis="gross",
)


def _read_csv(
    path: Path,
    *,
    label: str,
    snapshot_bytes: bytes | None = None,
) -> list[dict[str, str]]:
    resolved = path.expanduser().resolve()
    if snapshot_bytes is None and not resolved.is_file():
        raise FileNotFoundError(f"{label} is missing: {resolved}")
    payload_bytes = (
        bytes(snapshot_bytes) if snapshot_bytes is not None else resolved.read_bytes()
    )
    try:
        text = payload_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    return [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""))]


def _read_json(
    path: Path,
    *,
    label: str,
    snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if snapshot_bytes is None and not resolved.is_file():
        raise FileNotFoundError(f"{label} is missing: {resolved}")
    payload_bytes = (
        bytes(snapshot_bytes) if snapshot_bytes is not None else resolved.read_bytes()
    )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return dict(payload)


def _asof_rows(rows: Sequence[Mapping[str, Any]], asof: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        raw = str(row.get("asof_date") or "")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Transportation source row asof must be exact YYYY-MM-DD") from exc
        if parsed.isoformat() != raw:
            raise ValueError("Transportation source row asof must be exact YYYY-MM-DD")
        if raw == asof:
            result.append(dict(row))
    return result


def _max_asof(rows: Sequence[Mapping[str, Any]]) -> str:
    values = sorted(
        str(row.get("asof_date") or "")[:10]
        for row in rows
        if str(row.get("asof_date") or "")[:10]
    )
    return values[-1] if values else ""


def _manifest_hash(manifest: Mapping[str, Any], role: str) -> str:
    if role == "canonical_v8_score":
        artifacts = manifest.get("artifacts")
        if isinstance(artifacts, dict):
            score = artifacts.get("score_history")
            if isinstance(score, dict) and score.get("sha256"):
                return str(score["sha256"])
        return str(manifest.get("score_history_sha256") or "")
    if role == "canonical_v8_rank":
        artifacts = manifest.get("artifacts")
        if isinstance(artifacts, dict):
            rank = artifacts.get("rank_table")
            if isinstance(rank, dict) and rank.get("sha256"):
                return str(rank["sha256"])
        dashboard = manifest.get("dashboard")
        if isinstance(dashboard, dict) and dashboard.get("rank_table_sha256"):
            return str(dashboard["rank_table_sha256"])
        return str(manifest.get("rank_table_sha256") or "")
    raise KeyError(role)


def validate_fresh_sources(
    *,
    asof_date: str,
    capture_date: str,
    score_path: Path,
    rank_path: Path,
    source_manifest_path: Path,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    raw_asof = str(asof_date)
    raw_capture = str(capture_date)
    asof = date.fromisoformat(raw_asof)
    captured_on = date.fromisoformat(raw_capture)
    if asof.isoformat() != raw_asof or captured_on.isoformat() != raw_capture:
        raise ValueError("Transportation asof/capture dates must be exact YYYY-MM-DD")
    if captured_on != asof:
        raise ValueError("Transportation capture_date must exactly equal score/rank asof_date")
    if asof < POLICY_EFFECTIVE_FROM:
        raise ValueError("pre-effective Transportation artifacts are ineligible")
    if asof < FIRST_SIGNAL_DATE:
        raise ValueError("Transportation future-evidence clock has not started")
    if source_snapshot_bytes is not None and set(source_snapshot_bytes) != {
        "canonical_v8_score",
        "canonical_v8_rank",
        "source_manifest",
    }:
        raise ValueError("Transportation fresh-source snapshot role census changed")
    score_bytes = (
        source_snapshot_bytes["canonical_v8_score"]
        if source_snapshot_bytes is not None
        else None
    )
    rank_bytes = (
        source_snapshot_bytes["canonical_v8_rank"]
        if source_snapshot_bytes is not None
        else None
    )
    manifest_bytes = (
        source_snapshot_bytes["source_manifest"]
        if source_snapshot_bytes is not None
        else None
    )
    score_rows = _asof_rows(
        _read_csv(score_path, label="canonical v8 score", snapshot_bytes=score_bytes),
        asof.isoformat(),
    )
    rank_rows = _asof_rows(
        _read_csv(rank_path, label="canonical v8 rank", snapshot_bytes=rank_bytes),
        asof.isoformat(),
    )
    if not score_rows or not rank_rows:
        raise ValueError("canonical score and rank need exact asof rows")
    required_score_fields = {
        "ticker",
        "calibration_cohort",
        "v8_group_id",
        "ranking_mode",
        "v8_final_score",
        "v8_group_percentile_score",
        "source_rank_ready_flag",
        "group_cross_section_ready_flag",
        "group_specialized_ready_flag",
    }
    if any(required_score_fields - set(row) for row in score_rows):
        raise ValueError("canonical v8 score is missing required fields")
    score_tickers = [str(row["ticker"]).strip().upper() for row in score_rows]
    if len(score_tickers) != len(set(score_tickers)):
        raise ValueError("canonical v8 score has duplicate exact-date tickers")
    rank_tickers = [str(row.get("ticker") or "").strip().upper() for row in rank_rows]
    if len(rank_tickers) != len(set(rank_tickers)):
        raise ValueError("canonical v8 rank has duplicate exact-date tickers")
    if not set(score_tickers) <= set(rank_tickers):
        raise ValueError("canonical rank is missing v8 policy members")
    manifest = _read_json(
        source_manifest_path,
        label="canonical v8 source manifest",
        snapshot_bytes=manifest_bytes,
    )
    role = str(manifest.get("evidence_role") or "").lower()
    if not role or any(token in role for token in ("historical", "diagnostic", "revealed", "posthoc")):
        raise ValueError("historical/revealed diagnostic manifests cannot start the future clock")
    if str(manifest.get("asof_date") or "") != asof.isoformat():
        raise ValueError("canonical source manifest asof mismatch")
    score_sha = (
        hashlib.sha256(bytes(score_bytes)).hexdigest()
        if score_bytes is not None
        else file_sha256(score_path)
    )
    rank_sha = (
        hashlib.sha256(bytes(rank_bytes)).hexdigest()
        if rank_bytes is not None
        else file_sha256(rank_path)
    )
    if _manifest_hash(manifest, "canonical_v8_score") != score_sha:
        raise ValueError("canonical source manifest does not bind exact v8 score bytes")
    if _manifest_hash(manifest, "canonical_v8_rank") != rank_sha:
        raise ValueError("canonical source manifest does not bind exact v8 rank bytes")
    if manifest.get("historical_results_can_authorize_production") is not False:
        raise ValueError("canonical source manifest must preserve historical fail-closed governance")
    if manifest.get("production_activation_authorized") is not False:
        raise ValueError("source generation cannot self-authorize production")
    return score_rows, rank_rows, manifest


def normalize_signal_rows(score_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Apply the frozen v8 group selection rule without reading outcomes."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in score_rows:
        row = dict(raw)
        sleeve = str(row["calibration_cohort"])
        group = str(row["v8_group_id"])
        grouped.setdefault((sleeve, group), []).append(row)
    signals: list[dict[str, Any]] = []
    coverage: dict[str, bool] = {}
    for (sleeve, group), rows in sorted(grouped.items()):
        ranked = sorted(
            rows,
            key=lambda row: (
                -float(row["v8_group_percentile_score"]),
                str(row["ticker"]),
            ),
        )
        ranking_mode = str(ranked[0]["ranking_mode"])
        if any(str(row["ranking_mode"]) != ranking_mode for row in ranked):
            raise ValueError(f"{group}: mixed ranking modes")
        eligible = [
            row
            for row in ranked
            if int(row["source_rank_ready_flag"]) == 1
            and int(row["group_cross_section_ready_flag"]) == 1
        ]
        selection_count = max(1, math.ceil(0.20 * len(eligible))) if eligible else 0
        top = {str(row["ticker"]).upper() for row in eligible[:selection_count]}
        bottom = {str(row["ticker"]).upper() for row in eligible[-selection_count:]}
        if ranking_mode == "eligibility_equal_weight":
            top = set()
            bottom = set()
        for rank, row in enumerate(ranked, start=1):
            ticker = str(row["ticker"]).strip().upper()
            eligible_flag = int(row in eligible)
            signals.append(
                {
                    "asof_date": str(row["asof_date"])[:10],
                    "ticker": ticker,
                    "sleeve_id": sleeve,
                    "group_id": group,
                    "score": float(row["v8_group_percentile_score"]),
                    "raw_v8_final_score": float(row["v8_final_score"]),
                    "rank": rank,
                    "ranking_mode": ranking_mode,
                    "eligible_flag": eligible_flag,
                    "selected_top_flag": int(ticker in top),
                    "selected_bottom_flag": int(ticker in bottom),
                }
            )
        if sleeve == "oil_tanker_operators_v5":
            coverage[sleeve] = bool(eligible) and all(
                int(row["group_specialized_ready_flag"]) == 1 for row in eligible
            )
        else:
            coverage.setdefault(sleeve, True)
    return signals, coverage


def build_preflight(
    *,
    preflight_date: str,
    score_path: Path,
    rank_path: Path,
    source_manifest_path: Path | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    score_rows: list[dict[str, Any]] = []
    rank_rows: list[dict[str, Any]] = []
    score_max = rank_max = ""
    try:
        score_rows = _read_csv(score_path, label="canonical v8 score")
        score_max = _max_asof(score_rows)
    except (FileNotFoundError, ValueError) as exc:
        blockers.append(str(exc))
    try:
        rank_rows = _read_csv(rank_path, label="canonical v8 rank")
        rank_max = _max_asof(rank_rows)
    except (FileNotFoundError, ValueError) as exc:
        blockers.append(str(exc))
    preflight = date.fromisoformat(str(preflight_date)[:10])
    exact_identity = bool(score_max and rank_max and score_max == rank_max)
    clock_started = exact_identity and date.fromisoformat(score_max) >= FIRST_SIGNAL_DATE
    fresh = clock_started and 0 <= (preflight - date.fromisoformat(score_max)).days <= 1
    if not exact_identity:
        blockers.append("canonical v8 score/rank do not share one exact asof date")
    if exact_identity and not clock_started:
        blockers.append("latest canonical v8 score/rank is pre-first-signal")
    if clock_started and not fresh:
        blockers.append("canonical v8 score/rank is stale for contemporaneous capture")
    manifest_valid = False
    if clock_started and fresh and source_manifest_path is not None:
        try:
            validate_fresh_sources(
                asof_date=score_max,
                capture_date=score_max,
                score_path=score_path,
                rank_path=rank_path,
                source_manifest_path=source_manifest_path,
            )
            manifest_valid = True
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            blockers.append(str(exc))
    elif clock_started and fresh:
        blockers.append("fresh canonical v8 source manifest is missing")
    ready = clock_started and fresh and manifest_valid and not blockers
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "transportation_v7_future_oos_preflight",
        "preflight_date": preflight.isoformat(),
        "policy_effective_from": POLICY_EFFECTIVE_FROM.isoformat(),
        "first_signal_date": FIRST_SIGNAL_DATE.isoformat(),
        "latest_score_asof": score_max,
        "latest_rank_asof": rank_max,
        "capture_date_asof_identity_pass": exact_identity,
        "freshness_pass": fresh,
        "source_manifest_pass": manifest_valid,
        "status": "ready_for_signal_capture" if ready else "clock_not_started",
        "clock_started": clock_started,
        "blockers": blockers,
        "governing_counts": {21: 12, 63: 4},
        "script42_diagnostic_can_authorize": False,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "next_data_needed": [
            "fresh canonical v8 score and rank on one asof date >= 2026-08-24",
            "forward-shadow source manifest binding exact score/rank bytes",
            "independently anchored same-date signal capture",
            "12 nonoverlapping 21-session and 4 nonoverlapping 63-session outcomes per sleeve/group",
            "tanker specialized coverage pass on every counted capture",
        ],
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def capture_signal(
    *,
    asof_date: str,
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    trusted_capture_receipt_path: Path,
    expected_trusted_capture_receipt_sha256: str,
    trusted_capture_receipt_verifier: TrustedReceiptVerifier | None,
) -> dict[str, Any]:
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES:
        raise ValueError("Transportation capture source roles do not exactly match the contract")
    score_rows, _, _ = validate_fresh_sources(
        asof_date=asof_date,
        capture_date=asof_date,
        score_path=capture_source_paths["canonical_v8_score"],
        rank_path=capture_source_paths["canonical_v8_rank"],
        source_manifest_path=capture_source_paths["source_manifest"],
    )
    signals, sleeve_coverage = normalize_signal_rows(score_rows)
    payload = build_capture_payload(
        policy=TRANSPORT_POLICY,
        asof_date=asof_date,
        capture_date=asof_date,
        signal_rows=signals,
        source_paths=capture_source_paths,
        expected_source_sha256=expected_capture_source_sha256,
        required_source_roles=REQUIRED_CAPTURE_ROLES,
        trusted_receipt_path=trusted_capture_receipt_path,
        expected_trusted_receipt_sha256=expected_trusted_capture_receipt_sha256,
        trusted_receipt_verifier=trusted_capture_receipt_verifier,
    )
    payload["sleeve_coverage_gates"] = sleeve_coverage
    payload.pop("capture_id")
    payload.pop("payload_sha256")
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def evaluate(
    *,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    return evaluate_future_evidence(
        policy=TRANSPORT_POLICY,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        evaluation_at_utc=evaluation_at_utc,
    )


__all__ = [
    "FIRST_SIGNAL_DATE",
    "POLICY_EFFECTIVE_FROM",
    "POLICY_ID",
    "REQUIRED_CAPTURE_ROLES",
    "TRANSPORT_POLICY",
    "build_preflight",
    "capture_signal",
    "evaluate",
    "normalize_signal_rows",
    "validate_fresh_sources",
]
