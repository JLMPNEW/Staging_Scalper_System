#!/usr/bin/env python3
"""Freeze the fail-closed v7 research decision after the v5 forensic audit."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DEFAULT_AUDIT = (
    ROOT / "model_forensic_audit_v7" / "2026-08-21"
    / "transportation_v5_model_forensic_audit.json"
)
DEFAULT_TANKER_COVERAGE = (
    ROOT / "tanker_coverage_strict" / "2026-08-13"
    / "transportation_tanker_v5_coverage_by_metric.csv"
)
DEFAULT_OUTPUT_DIR = ROOT / "research_decision_v7" / "2026-08-21"
ACTION_FIELDS = (
    "priority", "cohort_id", "work_item", "scope", "authorization",
    "entry_gate", "exit_gate", "rebuild_effect",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forensic-audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--tanker-coverage", type=Path, default=DEFAULT_TANKER_COVERAGE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def action(
    priority: int,
    cohort_id: str,
    work_item: str,
    scope: str,
    authorization: str,
    entry_gate: str,
    exit_gate: str,
    rebuild_effect: str,
) -> dict[str, object]:
    return {
        "priority": priority,
        "cohort_id": cohort_id,
        "work_item": work_item,
        "scope": scope,
        "authorization": authorization,
        "entry_gate": entry_gate,
        "exit_gate": exit_gate,
        "rebuild_effect": rebuild_effect,
    }


def build_actions(
    audit: Mapping[str, object],
    tanker_coverage: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    targets = list(audit["summary"]["targeted_research_extraction_candidates"])
    near_tanker = sorted(
        str(row["metric_id"]) for row in tanker_coverage
        if str(row.get("history_gate")) == "PASS"
        and int(float(str(row.get("accepted_ticker_count") or 0))) + 1
        >= int(float(str(row.get("minimum_accepted_breadth") or 0)))
        and str(row.get("breadth_gate")) != "PASS"
    )
    return [
        action(
            1, "shared_transportation_research", "freeze_v7_model_specification",
            "subgroup-normalized surface architecture and cycle-aware tanker architecture",
            "AUTHORIZED_RESEARCH_ONLY",
            "38y mechanical tie-out PASS",
            "immutable spec hash plus fixed membership, transforms, directions, and future gates",
            "NO_HISTORICAL_OR_PRODUCTION_REBUILD",
        ),
        action(
            2, "north_american_surface_freight_and_logistics_v5",
            "accepted_history_incremental_proof",
            ",".join(targets) if targets else "none",
            "AUTHORIZED_RESEARCH_ONLY",
            "definitions frozen before replay; true comparison domains retained",
            "positive non-overlap IC and positive top-minus-cohort in at least two calendar blocks",
            "NO_REBUILD; DECIDES WHETHER TARGETED EXTRACTION IS WORTHWHILE",
        ),
        action(
            3, "oil_tanker_operators_v5", "accepted_history_cycle_feature_replay",
            ",".join(near_tanker) if near_tanker else "none",
            "AUTHORIZED_ACCEPTED_FACT_REPLAY_ONLY",
            "no network and no parser; accepted facts and filing dates only",
            "PIT rate-momentum or utilization exposure built for at least 8 issuers without lookahead",
            "NO_REBUILD; DECIDES WHETHER TARGETED EXTRACTION IS WORTHWHILE",
        ),
        action(
            4, "north_american_surface_freight_and_logistics_v5",
            "one_time_targeted_metric_extraction",
            ",".join(targets) if targets else "none",
            "CONDITIONAL_NOT_YET_AUTHORIZED",
            "priority-2 incremental proof PASS and exact missing issuer-period queue frozen",
            "semantic validation PASS plus required breadth/depth; parser runs once over frozen queue",
            "ONE RESEARCH PANEL REBUILD ONLY",
        ),
        action(
            5, "oil_tanker_operators_v5", "one_time_targeted_cycle_metric_extraction",
            ",".join(near_tanker) if near_tanker else "none",
            "CONDITIONAL_NOT_YET_AUTHORIZED",
            "priority-3 accepted-fact replay PASS and exact issuer-period queue frozen",
            "semantic validation PASS plus 8-issuer PIT coverage; parser runs once over frozen queue",
            "ONE RESEARCH PANEL REBUILD ONLY",
        ),
        action(
            6, "shared_transportation_research", "future_only_shadow_proof",
            "surface and tanker evaluated separately",
            "AUTHORIZED_AFTER_V7_SPEC_FREEZE",
            "v7 feature and score artifacts hash-bound; first eligible signal 2026-08-24",
            "12 non-overlapping monthly 21-session outcomes plus 4 non-overlapping 63-session outcomes; "
            "IC>0, top-minus-cohort>0, top-minus-bottom>0, hit-rate>=55%, no cohort block failure",
            "NO_PRODUCTION ACTIVATION UNTIL GATE PASSES",
        ),
    ]


def build_spec() -> dict[str, object]:
    return {
        "contract_version": "transportation_v7_research_specification_v1",
        "evidence_class": "outcome_informed_research_design",
        "production_authority": False,
        "design_freeze_date": "2026-08-21",
        "first_future_signal_date": "2026-08-24",
        "common_controls": {
            "point_in_time_filing_dates_required": True,
            "within_comparison_group_normalization_required": True,
            "missing_optional_metric_policy": "fixed_slot_neutral_no_cross_metric_renormalization",
            "selection": "top_20_percent_with_group_attribution",
            "cost_bps": 20,
            "overlap_control": "non_overlapping_outcomes_primary_hac_monthly_secondary",
            "historical_results_can_authorize_production": False,
        },
        "surface_freight": {
            "architecture": "hierarchical_comparison_group_scores_then_cohort_score",
            "comparison_groups": [
                "rail_networks", "ltl_carriers", "truckload_intermodal",
                "asset_light_logistics", "integrated_parcel",
            ],
            "do_not_expand": [
                "operating_ratio_level", "purchased_transportation_ratio_level",
            ],
            "research_features": [
                {
                    "metric_id": "operating_ratio_yoy_improvement",
                    "formula": "(prior_operating_ratio-current_operating_ratio)/abs(prior_operating_ratio)",
                    "direction": 1,
                    "domains": ["rail_networks", "ltl_carriers", "truckload_intermodal"],
                },
                {
                    "metric_id": "pricing_or_yield_growth",
                    "direction": 1,
                    "domains": ["ltl_carriers"],
                },
                {
                    "metric_id": "shipment_or_load_growth",
                    "direction": 1,
                    "domains": ["ltl_carriers"],
                },
                {
                    "metric_id": "freight_weight_per_shipment",
                    "direction": "research_only_unresolved_until_future_proof",
                    "domains": ["ltl_carriers"],
                },
            ],
        },
        "tankers": {
            "architecture": "cycle_aware_tanker_score",
            "remove_or_redefine": [
                "generic_fcf_yield", "generic_ev_operating_income", "asset_turnover",
            ],
            "research_features": [
                {
                    "metric_id": "tce_rate_yoy_growth",
                    "formula": "current_tce_day_rate/prior_year_tce_day_rate-1",
                    "direction": 1,
                    "source_first": "accepted_historical_facts",
                },
                {
                    "metric_id": "tce_cash_breakeven_spread",
                    "formula": "(tce_day_rate-cash_breakeven_per_day)/abs(cash_breakeven_per_day)",
                    "direction": 1,
                    "source_first": "accepted_historical_facts",
                },
                {
                    "metric_id": "fleet_utilization",
                    "direction": 1,
                    "source_first": "accepted_historical_facts",
                },
                {
                    "metric_id": "charter_coverage_next_12m",
                    "direction": "regime_interaction_not_unconditional",
                    "source_first": "accepted_historical_facts",
                },
                {
                    "metric_id": "net_debt_to_ebitda",
                    "direction": -1,
                    "source_first": "existing_financial_features",
                },
                {
                    "metric_id": "fleet_age",
                    "direction": -1,
                    "disposition": "do_not_expand_no_historical_signal",
                },
            ],
        },
        "promotion_gate": {
            "minimum_future_21_session_non_overlapping_outcomes": 12,
            "minimum_future_63_session_non_overlapping_outcomes": 4,
            "minimum_ic": 0.0,
            "minimum_top_minus_cohort_net": 0.0,
            "minimum_top_minus_bottom_gross": 0.0,
            "minimum_hit_rate": 0.55,
            "surface_minimum_cross_section": 20,
            "tanker_minimum_cross_section": 8,
            "cohort_isolation_required": True,
            "independent_promotion_readiness_audit_required": True,
        },
    }


def markdown(payload: Mapping[str, object]) -> str:
    lines = [
        "# Transportation v7 research decision",
        "",
        f"**Decision:** {payload['decision']}",
        "",
        "The v6 mechanics are verified. The next work is model redesign and accepted-fact proof, "
        "not a broad parser rerun and not another production calibration on revealed outcomes.",
        "",
        "## Ordered actions",
        "",
    ]
    for row in payload["actions"]:
        lines.extend(
            [
                f"{row['priority']}. **{row['work_item']}**  {row['authorization']}",
                f"   - Scope: {row['scope']}",
                f"   - Entry gate: {row['entry_gate']}",
                f"   - Exit gate: {row['exit_gate']}",
                f"   - Rebuild effect: {row['rebuild_effect']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Acceptance gates",
            "",
            "- No historical or production rebuild is authorized by this decision.",
            "- No network/parser work occurs until accepted-history incremental proof passes.",
            "- Any targeted extraction is one frozen issuer-period queue, followed by semantic validation once.",
            "- Surface freight and tankers remain separate; neither can fail or promote the other.",
            "- The first untouched v7 signal date is 2026-08-24.",
            "- Promotion remains fail-closed until the future-only gates in the specification pass.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if not args.forensic_audit.exists() or not args.tanker_coverage.exists():
        raise FileNotFoundError("forensic audit and strict tanker coverage are required")
    audit = read_json(args.forensic_audit)
    if str(audit["summary"]["mechanical_tieout"]) != "PASS":
        raise ValueError("v7 research decision is blocked by failed mechanical tie-out")
    tanker_coverage = read_csv(args.tanker_coverage)
    actions = build_actions(audit, tanker_coverage)
    spec = build_spec()
    payload: dict[str, object] = {
        "decision": "APPROVE_RESEARCH_SPEC_AND_ACCEPTED_FACT_REPLAY_ONLY",
        "production_activation_authorized": False,
        "broad_parser_run_authorized": False,
        "historical_recalibration_authorized": False,
        "actions": actions,
        "research_specification": spec,
        "lineage": {
            "forensic_audit_path": str(args.forensic_audit.resolve()),
            "forensic_audit_sha256": file_sha256(args.forensic_audit),
            "tanker_coverage_path": str(args.tanker_coverage.resolve()),
            "tanker_coverage_sha256": file_sha256(args.tanker_coverage),
        },
        "network_requests": 0,
        "parser_invocations": 0,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "transportation_v7_research_decision.json"
    csv_path = args.output_dir / "transportation_v7_action_queue.csv"
    md_path = args.output_dir / "TRANSPORTATION_V7_RESEARCH_DECISION.md"
    write_csv_atomic(csv_path, ACTION_FIELDS, actions)
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_text_atomic(md_path, markdown(payload))
    print(json.dumps({
        "decision": payload["decision"],
        "action_count": len(actions),
        "production_activation_authorized": False,
        "broad_parser_run_authorized": False,
        "historical_recalibration_authorized": False,
        "completed_action": actions[0]["work_item"],
        "next_action": actions[1]["work_item"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
