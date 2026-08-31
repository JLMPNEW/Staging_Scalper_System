"""Pinned-authority, exact-rank canonical Transportation capture."""

from __future__ import annotations

import csv
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from future_only_evidence.authority_config import (
    DEFAULT_AUTHORITY_REGISTRY,
    load_pinned_authority,
)
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import (
    PROSPECTIVE_ROLE,
    ProspectiveContract,
    build_strict_capture,
)

from .future_oos_capture_v2 import (
    validate_governing_contracts,
    validate_membership_snapshot,
)
from .future_oos_capture_v4 import REQUIRED_CAPTURE_ROLES_V4
from .future_oos_protocol_v1 import (
    FIRST_SIGNAL_DATE,
    POLICY_EFFECTIVE_FROM,
    POLICY_ID,
    validate_fresh_sources,
)


SOURCE_GENERATION_STATE = "canonical_v8_outcome_blind_frozen_before_entry"
CANONICAL_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")
CANONICAL_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
FROZEN_GROUP_WEIGHTS = {
    "north_american_surface_freight_and_logistics_v5": {
        "rail_networks": 0.25,
        "ltl_carriers": 0.25,
        "truckload_intermodal": 0.25,
        "asset_light_logistics": 0.15,
        "integrated_parcel": 0.10,
    },
    "oil_tanker_operators_v5": {"oil_tankers": 1.0},
}
FROZEN_GROUP_MODES = {
    "rail_networks": "ranked",
    "ltl_carriers": "ranked",
    "truckload_intermodal": "ranked",
    "asset_light_logistics": "ranked",
    "integrated_parcel": "eligibility_equal_weight",
    "oil_tankers": "ranked",
}


def _canonical_decimal(value: Any, *, label: str) -> float:
    if type(value) is not str or CANONICAL_DECIMAL.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical decimal string")
    parsed = float(value)
    if not math.isfinite(parsed) or (parsed == 0.0 and value.startswith("-")):
        raise ValueError(f"{label} must be a finite canonical decimal string")
    return parsed
TRANSPORT_CONTRACT = ProspectiveContract(
    family="transportation",
    policy_id=POLICY_ID,
    effective_from=POLICY_EFFECTIVE_FROM,
    first_signal_date=FIRST_SIGNAL_DATE,
    horizons=(21, 63),
    minimum_counts={21: 12, 63: 4},
    benchmark_ticker="IYT",
    cadence_id="special_first_then_month_end_v1",
    minimum_ic=0.0,
    minimum_efficacy=0.0,
    minimum_top_minus_bottom=0.0,
    minimum_hit_rate=0.55,
    transaction_cost_bps=20.0,
    top_minus_bottom_basis="gross",
)


def _csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _policy_weights(path: Path) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    policy = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("Transportation v8 policy must be a mapping")
    weights: dict[str, dict[str, float]] = {}
    modes: dict[str, str] = {}
    for cohort in policy.get("cohorts", {}).values():
        sleeve = str(cohort["calibration_cohort"])
        weights[sleeve] = {
            str(group): float(weight)
            for group, weight in cohort["aggregate_group_weights"].items()
        }
        for group, definition in cohort["groups"].items():
            modes[str(group)] = str(definition["ranking_mode"])
    if weights != FROZEN_GROUP_WEIGHTS or modes != FROZEN_GROUP_MODES:
        raise ValueError(
            "Transportation YAML group weights/modes differ from code-frozen v8 recipes"
        )
    return weights, modes


def derive_transport_signals(
    *,
    score_rows: Sequence[Mapping[str, Any]],
    rank_rows: Sequence[Mapping[str, Any]],
    group_weights: Mapping[str, Mapping[str, float]],
    group_modes: Mapping[str, str],
    asof_date: str,
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    expected_asof = str(asof_date)
    if date.fromisoformat(expected_asof).isoformat() != expected_asof:
        raise ValueError("Transportation signal asof must be exact YYYY-MM-DD")
    rank_index = {str(row.get("ticker") or "").strip().upper(): dict(row) for row in rank_rows}
    if len(rank_index) != len(rank_rows):
        raise ValueError("Transportation rank table has blank/duplicate tickers")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for raw in score_rows:
        row = dict(raw)
        ticker = str(row["ticker"]).strip().upper()
        if ticker in seen:
            raise ValueError("Transportation score ticker appears more than once at capture")
        seen.add(ticker)
        sleeve = str(row["calibration_cohort"])
        group = str(row["v8_group_id"])
        if sleeve not in group_weights or group not in group_weights[sleeve]:
            raise ValueError(f"{ticker}: score row is outside frozen group weights")
        rank = rank_index.get(ticker)
        if rank is None:
            raise ValueError(f"{ticker}: canonical rank row is missing")
        expected_rank_identity = {
            "asof_date": expected_asof,
            "transportation_cohort_id": str(row["v8_cohort_id"]),
            "transportation_group_id": group,
            "transportation_group_ranking_mode": str(row["ranking_mode"]),
        }
        for field, expected in expected_rank_identity.items():
            actual = str(rank.get(field) or "")
            if actual != expected:
                raise ValueError(f"{ticker}: score/rank identity mismatch: {field}")
        if str(row["ranking_mode"]) != group_modes[group]:
            raise ValueError(f"{ticker}: score ranking mode differs from frozen policy")
        published_weight = _canonical_decimal(
            rank["transportation_group_aggregate_weight"],
            label=f"{ticker} published group weight",
        )
        if abs(published_weight - float(group_weights[sleeve][group])) > 1e-12:
            raise ValueError(f"{ticker}: rank row group weight differs from frozen policy")
        grouped.setdefault((sleeve, group), []).append(row)
    if set(rank_index) != seen:
        raise ValueError("Transportation score/rank ticker census is not exact")
    expected_scopes = {
        (sleeve, group)
        for sleeve, weights in FROZEN_GROUP_WEIGHTS.items()
        for group in weights
    }
    if set(grouped) != expected_scopes:
        raise ValueError("Transportation capture lacks the exact frozen sleeve/group census")
    signals: list[dict[str, Any]] = []
    coverage: dict[str, bool] = {}
    for (sleeve, group), rows in sorted(grouped.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                -_canonical_decimal(
                    row["v8_group_percentile_score"],
                    label=f"{row.get('ticker')} v8 group percentile score",
                ),
                str(row["ticker"]),
            ),
        )
        mode = group_modes[group]
        for row in ordered:
            if str(row["v8_calibration_eligible_flag"]) not in {"0", "1"}:
                raise ValueError("Transportation calibration eligibility must be CSV 0/1")
        eligible = [row for row in ordered if row["v8_calibration_eligible_flag"] == "1"]
        if mode == "ranked" and len(eligible) < 2:
            raise ValueError(f"{group}: ranked group lacks an eligible cross-section")
        selection_count = max(1, math.ceil(0.20 * len(eligible))) if eligible else 0
        top = {str(row["ticker"]).upper() for row in eligible[:selection_count]}
        bottom = {str(row["ticker"]).upper() for row in eligible[-selection_count:]}
        if mode == "eligibility_equal_weight":
            top = set()
            bottom = set()
        for position, row in enumerate(ordered, start=1):
            ticker = str(row["ticker"]).strip().upper()
            rank = rank_index[ticker]
            published_rank = rank["transportation_group_rank"]
            if (
                type(published_rank) is not str
                or CANONICAL_POSITIVE_INTEGER.fullmatch(published_rank) is None
                or int(published_rank) != position
            ):
                raise ValueError(f"{ticker}: published group rank differs from v8 score order")
            published_score = _canonical_decimal(
                rank.get("portfolio_candidate_score"),
                label=f"{ticker} published portfolio candidate score",
            )
            group_score = _canonical_decimal(
                row["v8_group_percentile_score"],
                label=f"{ticker} v8 group percentile score",
            )
            final_score = _canonical_decimal(
                row["v8_final_score"],
                label=f"{ticker} v8 final score",
            )
            if abs(published_score - group_score) > 1e-8:
                raise ValueError(f"{ticker}: published rank score differs from canonical v8 score")
            eligible_flag = int(row in eligible)
            signals.append(
                {
                    "asof_date": expected_asof,
                    "ticker": ticker,
                    "sleeve_id": sleeve,
                    "group_id": group,
                    "score": group_score,
                    "raw_v8_final_score": final_score,
                    "rank": position,
                    "ranking_mode": mode,
                    "eligible_flag": eligible_flag,
                    "predictive_eligible_flag": int(eligible_flag and mode == "ranked"),
                    "selected_top_flag": int(ticker in top),
                    "selected_bottom_flag": int(ticker in bottom),
                    "frozen_group_weight": float(group_weights[sleeve][group]),
                    "score_source_row_sha256": canonical_sha256(row),
                    "rank_source_row_sha256": canonical_sha256(rank),
                }
            )
        if sleeve == "oil_tanker_operators_v5":
            if any(
                str(row["group_specialized_ready_flag"]) not in {"0", "1"}
                for row in eligible
            ):
                raise ValueError("Transportation specialized readiness must be CSV 0/1")
            coverage[sleeve] = bool(eligible) and all(
                row["group_specialized_ready_flag"] == "1" for row in eligible
            )
        else:
            coverage.setdefault(sleeve, True)
    return signals, coverage


def capture_signal(
    *,
    asof_date: str,
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    capture_receipt_path: Path,
    expected_capture_receipt_sha256: str,
    trusted_public_key_path: Path,
    authority_registry_path: Path = DEFAULT_AUTHORITY_REGISTRY,
) -> dict[str, Any]:
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V4:
        raise ValueError("Transportation canonical capture source roles changed")
    authority, authority_audit = load_pinned_authority(
        "transportation",
        public_key_path=trusted_public_key_path,
        registry_path=authority_registry_path,
    )
    governance = validate_governing_contracts(
        v8_policy_path=capture_source_paths["v8_policy"],
        v7_research_decision_path=capture_source_paths["v7_research_decision"],
    )
    membership = validate_membership_snapshot(
        asof_date=asof_date,
        membership_path=capture_source_paths["membership_snapshot"],
        score_path=capture_source_paths["canonical_v8_score"],
        rank_path=capture_source_paths["canonical_v8_rank"],
        source_manifest_path=capture_source_paths["source_manifest"],
    )
    score_rows, rank_rows, manifest = validate_fresh_sources(
        asof_date=asof_date,
        capture_date=asof_date,
        score_path=capture_source_paths["canonical_v8_score"],
        rank_path=capture_source_paths["canonical_v8_rank"],
        source_manifest_path=capture_source_paths["source_manifest"],
    )
    if manifest.get("evidence_role") != PROSPECTIVE_ROLE:
        raise ValueError("Transportation source manifest role must be exact prospective capture")
    if manifest.get("source_generation_state") != SOURCE_GENERATION_STATE:
        raise ValueError("Transportation source manifest is not outcome-blind frozen state")
    if manifest.get("return_target") != {
        "benchmark_ticker": "IYT",
        "return_convention": "next_session_open_execution_total_return_v1",
        "target_field": "forward_iyt_excess_return",
    }:
        raise ValueError("Transportation source manifest changed the IYT-excess target")
    weights, modes = _policy_weights(capture_source_paths["v8_policy"])
    signals, coverage = derive_transport_signals(
        score_rows=score_rows,
        rank_rows=rank_rows,
        group_weights=weights,
        group_modes=modes,
        asof_date=asof_date,
    )
    payload = build_strict_capture(
        contract=TRANSPORT_CONTRACT,
        asof_date=asof_date,
        signal_rows=signals,
        source_paths=capture_source_paths,
        expected_source_sha256=expected_capture_source_sha256,
        required_source_roles=REQUIRED_CAPTURE_ROLES_V4,
        trading_calendar_path=capture_source_paths["trading_calendar"],
        capture_receipt_path=capture_receipt_path,
        expected_capture_receipt_sha256=expected_capture_receipt_sha256,
        authority=authority,
        domain_fields={
            "domain_schema_version": "transportation_future_only_signal_capture_v5",
            "target_field": "forward_iyt_excess_return",
            "sleeve_coverage_gates": coverage,
            "frozen_group_weights": weights,
            "group_ranking_modes": modes,
            "governing_contract_audit": governance,
            "membership_audit": membership,
            "trusted_authority_audit": {
                **authority_audit,
                "authority_registry_sha256": file_sha256(authority_registry_path),
            },
            "parcel_predictive_applicability": "not_applicable_monitor_only",
        },
    )
    return payload


__all__ = [
    "FROZEN_GROUP_MODES",
    "FROZEN_GROUP_WEIGHTS",
    "SOURCE_GENERATION_STATE",
    "TRANSPORT_CONTRACT",
    "capture_signal",
    "derive_transport_signals",
]
