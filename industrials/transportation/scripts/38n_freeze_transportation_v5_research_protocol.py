#!/usr/bin/env python3
"""Freeze outcome-blind v5 candidate registries and research rules."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    candidate_registry_from_policy,
    load_cohort_score_policy,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_SCORE_VALIDATION = ROOT / "investable_v5" / "pit_score_validation" / "2026-08-15" / "transportation_v5_pit_score_history_validation.json"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "research_protocol" / "2026-08-15"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-validation", type=Path, default=DEFAULT_SCORE_VALIDATION)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main() -> int:
    args = parse_args()
    validation_path = args.score_validation.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    policy_paths = (
        args.surface_policy.expanduser().resolve(),
        args.tanker_policy.expanduser().resolve(),
    )
    validation = read_json(validation_path)
    if validation.get("acceptance") != "PASS" or not validation.get(
        "bounded_research_calibration_authorized"
    ):
        raise ValueError("PIT score-history validation has not authorized research")
    policies = tuple(load_cohort_score_policy(path) for path in policy_paths)
    cohort_results = dict(validation.get("cohort_results") or {})
    registries: dict[str, Any] = {}
    for policy, path in zip(policies, policy_paths):
        cohort = str(policy["cohort_id"])
        positioning_enabled = (
            str(
                (cohort_results.get(cohort) or {}).get(
                    "positioning_candidate_history_gate"
                )
                or ""
            )
            == "PASS"
        )
        candidates = candidate_registry_from_policy(
            policy, positioning_enabled=positioning_enabled
        )
        if len(candidates) < 2:
            raise ValueError(f"{cohort}: fewer than two enabled candidates")
        registries[cohort] = {
            "policy_path": str(path),
            "policy_sha256": file_sha256(path),
            "policy_version": str(policy["policy_version"]),
            "minimum_cross_section": int(policy["minimum_active_cohort_size"]),
            "positioning_candidate_enabled": positioning_enabled,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
    result = {
        "acceptance": "PASS",
        "contract_version": "transportation_v5_outcome_blind_research_protocol_v2",
        "score_history_validation_path": str(validation_path),
        "score_history_validation_sha256": file_sha256(validation_path),
        "candidate_registries": registries,
        "evaluation": {
            "horizons_sessions": [21, 63],
            "primary_horizon_sessions": 63,
            "selection_fraction": 0.20,
            "transaction_cost_bps": 20.0,
            "calendar_blocks": [
                {
                    "block_id": "diagnostic_block_1",
                    "start_date": "2019-01-01",
                    "end_date": "2021-12-31",
                },
                {
                    "block_id": "diagnostic_block_2",
                    "start_date": "2022-01-01",
                    "end_date": "2023-12-31",
                },
                {
                    "block_id": "diagnostic_block_3",
                    "start_date": "2024-01-01",
                    "end_date": "2026-07-30",
                },
            ],
            "minimum_non_overlapping_snapshots_per_block": 6,
            "aggregate_history_role": "descriptive_only",
            "ranking_gate_metrics": [
                "mean_ic",
                "mean_top_minus_cohort_net",
                "mean_top_minus_bottom_gross",
            ],
            "investability_gate_metrics": [
                "mean_top_excess_net",
                "top_excess_hit_rate",
            ],
            "require_complete_components": False,
            "return_basis": "next_session_open_execution_excess",
            "benchmark_ticker": "IYT",
            "cohort_isolation_required": True,
            "independent_return_reconstruction_required": True,
            "price_slice_hash_required": True,
        },
        "evidence_governance": {
            "historical_panel_class": "diagnostic_only_pre_freeze_or_previously_revealed",
            "historical_results_can_authorize_production": False,
            "candidate_selection_can_authorize_production": False,
            "future_proof_cutoff_date": "2026-07-30",
            "first_future_signal_date": "2026-07-31",
            "promotion_requires_future_untouched_evidence": True,
            "membership_selection_uses_outcomes": False,
            "candidate_design_uses_outcomes": False,
        },
        "network_requests": 0,
        "parser_invocations": 0,
        "outcomes_accessed": False,
        "historical_diagnostic_calibration_authorized": True,
        "production_activation_authorized": False,
        "next_gate": "BUILD_AND_INDEPENDENTLY_RECONCILE_V5_OUTCOME_PANEL",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_v5_research_protocol.json"
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
