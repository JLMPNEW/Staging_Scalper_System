#!/usr/bin/env python3
"""Evaluate v8 comparison groups before any fixed-weight cohort aggregation."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_research import (  # noqa: E402
    evaluate_candidate,
    finite_float,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.subgroup_scoring import load_subgroup_score_policy  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
DEFAULT_SCORE_MANIFEST = (
    ROOT
    / "investable_v5"
    / "subgroup_scores_v8"
    / "2026-08-21"
    / "transportation_v8_subgroup_score_history.json"
)
DEFAULT_PANEL = (
    ROOT
    / "investable_v5"
    / "outcome_panel_v6"
    / "2026-08-16"
    / "transportation_v5_outcome_panel.csv"
)
DEFAULT_PANEL_MANIFEST = DEFAULT_PANEL.parent / "transportation_v5_outcome_panel_manifest.json"
DEFAULT_OUTPUT_ROOT = (
    ROOT / "investable_v5" / "subgroup_calibration_v8_independent_v3"
)

RESULT_FIELDS = (
    "cohort_id",
    "group_id",
    "ranking_mode",
    "horizon_sessions",
    "evaluation_block",
    "eligible_row_count",
    "available_outcome_row_count",
    "outcome_coverage",
    "snapshot_count",
    "mean_ic",
    "mean_top_excess_net",
    "top_excess_hit_rate",
    "mean_top_minus_cohort_net",
    "mean_top_minus_bottom_gross",
    "non_overlapping_snapshot_count",
    "independent_eligible_row_count",
    "independent_available_outcome_row_count",
    "independent_outcome_coverage",
    "mean_independent_ic",
    "mean_independent_top_excess_net",
    "independent_top_excess_hit_rate",
    "mean_independent_top_minus_cohort_net",
    "mean_independent_top_minus_bottom_gross",
    "average_independent_turnover",
    "invalid_execution_interval_cross_section_count",
    "early_terminal_observation_count",
    "late_security_entry_observation_count",
    "ranking_gate",
    "investability_gate",
    "effective_sample_gate",
    "group_gate",
    "evidence_role",
)
PERIOD_FIELDS = (
    "cohort_id",
    "group_id",
    "horizon_sessions",
    "evaluation_block",
    "asof_date",
    "entry_date",
    "exit_date",
    "cross_section",
    "selected",
    "selected_tickers_json",
    "bottom_tickers_json",
    "ic",
    "turnover",
    "gross_excess",
    "net_excess",
    "cohort_excess",
    "bottom_excess",
    "top_minus_cohort_gross",
    "top_minus_cohort_net",
    "top_minus_bottom_gross",
    "early_terminal_observation_count",
    "terminal_proceeds_policy",
    "late_security_entry_observation_count",
    "late_security_entry_policy",
)
SCHEDULE_FIELDS = (
    "cohort_id",
    "group_id",
    "horizon_sessions",
    "evaluation_block",
    "independent_sequence",
    "asof_date",
    "entry_date",
    "exit_date",
    "eligible_row_count",
    "available_outcome_row_count",
    "evaluation_available_flag",
    "cross_section",
    "selected",
    "selected_tickers_json",
    "bottom_tickers_json",
    "turnover",
    "ic",
    "gross_excess",
    "net_excess",
    "cohort_excess",
    "bottom_excess",
    "top_minus_cohort_gross",
    "top_minus_cohort_net",
    "top_minus_bottom_gross",
    "early_terminal_observation_count",
    "terminal_proceeds_policy",
    "late_security_entry_observation_count",
    "late_security_entry_policy",
)
INVALID_INTERVAL_FIELDS = (
    "cohort_id",
    "group_id",
    "horizon_sessions",
    "asof_date",
    "eligible_row_count",
    "reasons_json",
    "tickers_json",
    "outcome_unavailable_reasons_json",
    "right_censored_at_panel_end_flag",
    "disposition",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="2026-08-21")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--score-manifest", type=Path, default=DEFAULT_SCORE_MANIFEST)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--panel-manifest", type=Path, default=DEFAULT_PANEL_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def fmt(value: object) -> str:
    parsed = finite_float(value)
    return "" if parsed is None else (f"{parsed:.12f}".rstrip("0").rstrip(".") or "0")


def _date_block_id(value: str, blocks: list[dict[str, object]]) -> str | None:
    matches = [
        str(item["block_id"])
        for item in blocks
        if str(item["start_date"]) <= value <= str(item["end_date"])
    ]
    if len(matches) > 1:
        raise ValueError(f"{value}: overlapping fixed diagnostic blocks")
    return matches[0] if matches else None


def block_id(asof: str, blocks: list[dict[str, object]]) -> str:
    matched = _date_block_id(asof, blocks)
    if matched is None:
        raise ValueError(f"{asof}: expected exactly one fixed diagnostic block")
    return matched


def strict_block_id(
    entry_date: str,
    exit_date: str,
    blocks: list[dict[str, object]],
) -> str | None:
    """Return a block only when the benchmark execution interval is contained."""
    signal_block = _date_block_id(entry_date, blocks)
    exit_block = _date_block_id(exit_date, blocks)
    return signal_block if signal_block is not None and signal_block == exit_block else None


def require_unique_rows(
    rows: list[dict[str, str]],
    *,
    key_fields: tuple[str, ...],
    label: str,
) -> None:
    seen: set[tuple[str, ...]] = set()
    duplicates: list[tuple[str, ...]] = []
    for row in rows:
        key = tuple(str(row.get(field) or "") for field in key_fields)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise ValueError(
            f"{label}: duplicate identities for {key_fields}: {duplicates[:5]}"
        )


def summarize_fixed_block(
    *,
    source_rows: list[dict[str, object]],
    horizon: int,
    block: str,
    blocks: list[dict[str, object]],
    top_fraction: float,
    minimum_cross_section: int,
    transaction_cost_bps: float,
) -> dict[str, object]:
    """Evaluate a contained block with an independent initial formation cost."""
    by_asof: dict[str, list[dict[str, object]]] = {}
    for row in source_rows:
        try:
            row_horizon = int(float(str(row.get("horizon_sessions") or 0)))
        except ValueError:
            continue
        if row_horizon != horizon:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        by_asof.setdefault(str(row.get("asof_date") or ""), []).append(
            dict(row)
        )
    candidates: list[dict[str, object]] = []
    for date_rows in by_asof.values():
        entries = {
            str(row.get("benchmark_entry_date") or "") for row in date_rows
        }
        exits = {
            str(row.get("benchmark_exit_date") or "") for row in date_rows
        }
        if (
            len(entries) != 1
            or len(exits) != 1
            or "" in entries
            or "" in exits
        ):
            continue
        if strict_block_id(next(iter(entries)), next(iter(exits)), blocks) != block:
            continue
        candidates.extend(dict(row, split=block) for row in date_rows)
    return evaluate_candidate(
        candidates,
        weights={"v8_score": 1.0},
        split=block,
        horizon_sessions=horizon,
        top_fraction=top_fraction,
        minimum_cross_section=minimum_cross_section,
        transaction_cost_bps=transaction_cost_bps,
        require_complete_components=True,
        require_unique_benchmark_interval=True,
    )


def gate_row(
    *,
    cohort_id: str,
    group_id: str,
    ranking_mode: str,
    horizon: int,
    block: str,
    metrics: Mapping[str, object],
    gates: Mapping[str, object],
) -> dict[str, str]:
    coverage = finite_float(metrics.get("independent_outcome_coverage")) or 0.0
    ic = finite_float(metrics.get("mean_independent_ic"))
    spread = finite_float(
        metrics.get("mean_independent_top_minus_cohort_net")
    )
    top_bottom = finite_float(
        metrics.get("mean_independent_top_minus_bottom_gross")
    )
    net = finite_float(metrics.get("mean_independent_top_excess_net"))
    hit = finite_float(metrics.get("independent_top_excess_hit_rate"))
    descriptive = block == "diagnostic_all"
    ranking_gate = bool(
        coverage >= 0.80
        and ic is not None
        and ic > float(gates["minimum_ic"])
        and spread is not None
        and spread > float(gates["minimum_top_minus_group_net"])
        and top_bottom is not None
        and top_bottom > float(gates["minimum_top_minus_bottom_gross"])
    )
    investability_gate = bool(
        net is not None
        and net > 0.0
        and hit is not None
        and hit >= float(gates["minimum_hit_rate"])
    )
    sample_gate = int(metrics.get("non_overlapping_snapshot_count") or 0) >= int(
        gates["minimum_non_overlapping_snapshots_per_block"]
    )
    passed = not descriptive and ranking_gate and investability_gate and sample_gate
    return {
        "cohort_id": cohort_id,
        "group_id": group_id,
        "ranking_mode": ranking_mode,
        "horizon_sessions": str(horizon),
        "evaluation_block": block,
        "eligible_row_count": str(metrics.get("eligible_row_count") or 0),
        "available_outcome_row_count": str(metrics.get("available_outcome_row_count") or 0),
        "outcome_coverage": fmt(metrics.get("outcome_coverage")),
        "snapshot_count": str(metrics.get("snapshot_count") or 0),
        "mean_ic": fmt(metrics.get("mean_ic")),
        "mean_top_excess_net": fmt(metrics.get("mean_top_excess_net")),
        "top_excess_hit_rate": fmt(metrics.get("top_excess_hit_rate")),
        "mean_top_minus_cohort_net": fmt(metrics.get("mean_top_minus_cohort_net")),
        "mean_top_minus_bottom_gross": fmt(metrics.get("mean_top_minus_bottom_gross")),
        "non_overlapping_snapshot_count": str(
            metrics.get("non_overlapping_snapshot_count") or 0
        ),
        "independent_eligible_row_count": str(
            metrics.get("independent_eligible_row_count") or 0
        ),
        "independent_available_outcome_row_count": str(
            metrics.get("independent_available_outcome_row_count") or 0
        ),
        "independent_outcome_coverage": fmt(
            metrics.get("independent_outcome_coverage")
        ),
        "mean_independent_ic": fmt(metrics.get("mean_independent_ic")),
        "mean_independent_top_excess_net": fmt(
            metrics.get("mean_independent_top_excess_net")
        ),
        "independent_top_excess_hit_rate": fmt(
            metrics.get("independent_top_excess_hit_rate")
        ),
        "mean_independent_top_minus_cohort_net": fmt(
            metrics.get("mean_independent_top_minus_cohort_net")
        ),
        "mean_independent_top_minus_bottom_gross": fmt(
            metrics.get("mean_independent_top_minus_bottom_gross")
        ),
        "average_independent_turnover": fmt(
            metrics.get("average_independent_turnover")
        ),
        "invalid_execution_interval_cross_section_count": str(
            metrics.get("invalid_execution_interval_cross_section_count") or 0
        ),
        "early_terminal_observation_count": str(
            metrics.get("early_terminal_observation_count") or 0
        ),
        "late_security_entry_observation_count": str(
            metrics.get("late_security_entry_observation_count") or 0
        ),
        "ranking_gate": "PASS" if ranking_gate else "FAIL",
        "investability_gate": "PASS" if investability_gate else "FAIL",
        "effective_sample_gate": "PASS" if sample_gate else "FAIL",
        "group_gate": "DESCRIPTIVE_ONLY" if descriptive else "PASS" if passed else "FAIL",
        "evidence_role": (
            "REVEALED_INDEPENDENT_SCHEDULE_DIAGNOSTIC_ONLY"
        ),
    }


def schedule_evidence_rows(
    *,
    cohort_id: str,
    group_id: str,
    horizon: int,
    block: str,
    metrics: Mapping[str, object],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in metrics.get("independent_schedule_rows") or []:
        item = dict(row)
        output.append(
            {
                "cohort_id": cohort_id,
                "group_id": group_id,
                "horizon_sessions": str(horizon),
                "evaluation_block": block,
                "independent_sequence": str(
                    item.get("independent_sequence") or ""
                ),
                "asof_date": str(item.get("asof_date") or ""),
                "entry_date": str(item.get("entry_date") or ""),
                "exit_date": str(item.get("exit_date") or ""),
                "eligible_row_count": str(
                    item.get("eligible_row_count") or 0
                ),
                "available_outcome_row_count": str(
                    item.get("available_outcome_row_count") or 0
                ),
                "evaluation_available_flag": str(
                    item.get("evaluation_available_flag") or 0
                ),
                "cross_section": str(item.get("cross_section") or 0),
                "selected": str(item.get("selected") or 0),
                "selected_tickers_json": json.dumps(
                    item.get("selected_tickers") or [],
                    separators=(",", ":"),
                ),
                "bottom_tickers_json": json.dumps(
                    item.get("bottom_tickers") or [],
                    separators=(",", ":"),
                ),
                **{
                    field: fmt(item.get(field))
                    for field in (
                        "turnover",
                        "ic",
                        "gross_excess",
                        "net_excess",
                        "cohort_excess",
                        "bottom_excess",
                        "top_minus_cohort_gross",
                        "top_minus_cohort_net",
                        "top_minus_bottom_gross",
                    )
                },
                "early_terminal_observation_count": str(
                    item.get("early_terminal_observation_count") or 0
                ),
                "terminal_proceeds_policy": (
                    "terminal_proceeds_cash_carry_to_benchmark_exit_zero_return"
                ),
                "late_security_entry_observation_count": str(
                    item.get("late_security_entry_observation_count") or 0
                ),
                "late_security_entry_policy": (
                    "cash_carry_from_benchmark_entry_to_"
                    "security_entry_zero_return"
                ),
            }
        )
    return output


def non_ranked_group_summary(ranking_mode: str) -> dict[str, object]:
    if ranking_mode != "eligibility_equal_weight":
        raise ValueError(f"unsupported non-ranked mode: {ranking_mode}")
    return {
        "status": "NON_PREDICTIVE_ELIGIBILITY_EQUAL_WEIGHT_SLEEVE",
        "predictive_gate_applicability": "NOT_APPLICABLE",
        "all_fixed_blocks_pass": None,
    }


def calibration_truth_labels(
    group_summary: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Separate successful execution from failed predictive acceptance."""
    predictive_failures = sorted(
        key
        for key, result in group_summary.items()
        if result.get("all_fixed_blocks_pass") is False
    )
    return {
        "execution_acceptance": "PASS",
        "predictive_acceptance": (
            "FAIL" if predictive_failures else "PASS"
        ),
        "predictive_failure_groups": predictive_failures,
        # Revealed-history diagnostics cannot authorize a production change,
        # even in a hypothetical run where every predictive block passes.
        "production_promotion_eligible": False,
    }


def main() -> int:
    args = parse_args()
    paths = {
        "policy": args.policy.expanduser().resolve(),
        "score_manifest": args.score_manifest.expanduser().resolve(),
        "panel": args.panel.expanduser().resolve(),
        "panel_manifest": args.panel_manifest.expanduser().resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing v8 calibration inputs={missing}")
    policy = load_subgroup_score_policy(paths["policy"])
    score_manifest = read_json(paths["score_manifest"])
    if score_manifest.get("acceptance") != "PASS":
        raise ValueError("v8 score regeneration is not PASS")
    if score_manifest.get("historical_score_regeneration_count") != 1:
        raise ValueError("v8 score history was not regenerated exactly once")
    if score_manifest.get("post_semantic_parser_invocations") != 0:
        raise ValueError("calibration cannot follow a second parser pass")
    bridge = score_manifest.get("conflict_resolution_bridge") or {}
    if (
        bridge.get("status") != "VERIFIED"
        or bridge.get("unresolved_conflicts_excluded_by_score_resolver")
        is not True
        or bridge.get("production_activation_authorized") is not False
    ):
        raise ValueError("v8 score history is not bound to a fail-closed audit")
    conflict_lineage = score_manifest.get("lineage", {}).get(
        "conflict_audit"
    ) or {}
    conflict_audit_path = Path(str(conflict_lineage.get("path") or ""))
    if (
        not conflict_audit_path.is_file()
        or file_sha256(conflict_audit_path)
        != str(conflict_lineage.get("sha256") or "")
    ):
        raise ValueError("v8 conflict-audit lineage is missing or changed")
    conflict_audit = read_json(conflict_audit_path)
    if conflict_audit.get("contract_version") != (
        "transportation_accepted_fact_conflict_resolution_v3"
    ):
        raise ValueError("calibration requires strict v3 conflict boundaries")
    paths["conflict_audit"] = conflict_audit_path
    if str(score_manifest["lineage"]["policy"]["sha256"]) != file_sha256(paths["policy"]):
        raise ValueError("v8 policy changed after score regeneration")
    score_path = Path(str(score_manifest["artifacts"]["score_history"]["path"]))
    if file_sha256(score_path) != str(
        score_manifest["artifacts"]["score_history"]["sha256"]
    ):
        raise ValueError("v8 score-history hash mismatch")
    panel_manifest = read_json(paths["panel_manifest"])
    if str(panel_manifest.get("panel_sha256") or "") != file_sha256(paths["panel"]):
        raise ValueError("immutable outcome panel hash mismatch")
    if panel_manifest.get("historical_results_can_authorize_production") is not False:
        raise ValueError("revealed panel cannot authorize production")
    if str(score_manifest["lineage"]["panel"]["sha256"]) != file_sha256(
        paths["panel"]
    ):
        raise ValueError("v8 score manifest is not bound to this outcome panel")
    if str(score_manifest["lineage"]["panel_manifest"]["sha256"]) != file_sha256(
        paths["panel_manifest"]
    ):
        raise ValueError("v8 score manifest is not bound to this panel manifest")

    score_rows = read_csv(score_path)
    panel_rows = read_csv(paths["panel"])
    require_unique_rows(
        score_rows,
        key_fields=("asof_date", "ticker"),
        label="v8 score history",
    )
    require_unique_rows(
        panel_rows,
        key_fields=("asof_date", "ticker", "horizon_sessions"),
        label="outcome panel",
    )
    scores = {(row["asof_date"], row["ticker"]): row for row in score_rows}
    joined: list[dict[str, object]] = []
    for panel_row in panel_rows:
        score = scores.get((panel_row["asof_date"], panel_row["ticker"]))
        if score is None:
            continue
        score_source_sha = str(score.get("source_score_sha256") or "")
        panel_source_sha = str(panel_row.get("source_score_sha256") or "")
        if not score_source_sha or score_source_sha != panel_source_sha:
            raise ValueError(
                "score/outcome evidence identity mismatch for "
                f"{panel_row['asof_date']}::{panel_row['ticker']}"
            )
        joined.append(
            {
                **panel_row,
                "v8_cohort_id": score["v8_cohort_id"],
                "v8_group_id": score["v8_group_id"],
                "ranking_mode": score["ranking_mode"],
                "v8_score": score["v8_group_percentile_score"],
                "calibration_eligible_flag": score["v8_calibration_eligible_flag"],
            }
        )

    gates = policy["calibration_gates"]
    blocks = [dict(item) for item in gates["fixed_calendar_blocks"]]
    block_ids = [str(item["block_id"]) for item in blocks]
    horizons = [int(item) for item in gates["horizons_sessions"]]
    result_rows: list[dict[str, str]] = []
    period_rows: list[dict[str, str]] = []
    independent_schedule_rows: list[dict[str, str]] = []
    invalid_interval_rows: list[dict[str, str]] = []
    excluded_cross_block_period_count = 0
    invalid_benchmark_interval_cross_section_count = 0
    early_terminal_observation_count = 0
    late_security_entry_observation_count = 0
    group_summary: dict[str, Any] = {}
    cohort_authorization = {
        str(key): bool(value)
        for key, value in score_manifest[
            "cohort_diagnostic_calibration_authorized"
        ].items()
    }

    for cohort_id, cohort in policy["cohorts"].items():
        cohort_ready = cohort_authorization.get(str(cohort_id), False)
        for group_id, group in cohort["groups"].items():
            key = f"{cohort_id}::{group_id}"
            ranking_mode = str(group["ranking_mode"])
            if ranking_mode != "ranked":
                group_summary[key] = non_ranked_group_summary(ranking_mode)
                continue
            if not cohort_ready:
                group_summary[key] = {
                    "status": "BLOCKED_REQUIRED_SPECIALIZED_PACK_COVERAGE",
                    "all_fixed_blocks_pass": False,
                }
                continue
            group_rows = [
                dict(row)
                for row in joined
                if row["v8_cohort_id"] == cohort_id and row["v8_group_id"] == group_id
            ]
            group_block_gates: list[str] = []
            for horizon in horizons:
                all_rows = [dict(row, split="diagnostic_all") for row in group_rows]
                metrics = evaluate_candidate(
                    all_rows,
                    weights={"v8_score": 1.0},
                    split="diagnostic_all",
                    horizon_sessions=horizon,
                    top_fraction=float(policy["aggregation"]["selection_fraction"]),
                    minimum_cross_section=int(group["minimum_cross_section"]),
                    transaction_cost_bps=float(gates["transaction_cost_bps"]),
                    require_complete_components=True,
                    require_unique_benchmark_interval=True,
                )
                invalid_benchmark_interval_cross_section_count += int(
                    metrics.get(
                        "invalid_execution_interval_cross_section_count"
                    )
                    or 0
                )
                early_terminal_observation_count += int(
                    metrics.get("early_terminal_observation_count") or 0
                )
                late_security_entry_observation_count += int(
                    metrics.get(
                        "late_security_entry_observation_count"
                    )
                    or 0
                )
                for failure in (
                    metrics.get(
                        "invalid_execution_interval_cross_sections"
                    )
                    or []
                ):
                    invalid_interval_rows.append(
                        {
                            "cohort_id": str(cohort_id),
                            "group_id": str(group_id),
                            "horizon_sessions": str(horizon),
                            "asof_date": str(
                                failure.get("asof_date") or ""
                            ),
                            "eligible_row_count": str(
                                failure.get("eligible_row_count") or 0
                            ),
                            "reasons_json": json.dumps(
                                failure.get("reasons") or [],
                                separators=(",", ":"),
                            ),
                            "tickers_json": json.dumps(
                                failure.get("tickers") or [],
                                separators=(",", ":"),
                            ),
                            "outcome_unavailable_reasons_json": (
                                json.dumps(
                                    failure.get(
                                        "outcome_unavailable_reasons"
                                    )
                                    or [],
                                    separators=(",", ":"),
                                )
                            ),
                            "right_censored_at_panel_end_flag": str(
                                failure.get(
                                    "right_censored_at_panel_end_flag"
                                )
                                or 0
                            ),
                            "disposition": (
                                "EXCLUDED_FAIL_CLOSED_NOT_GATED"
                            ),
                        }
                    )
                full_period_rows = [
                    dict(period) for period in (metrics.get("period_rows") or [])
                ]
                result_rows.append(
                    gate_row(
                        cohort_id=str(cohort_id),
                        group_id=str(group_id),
                        ranking_mode=ranking_mode,
                        horizon=horizon,
                        block="diagnostic_all",
                        metrics=metrics,
                        gates=gates,
                    )
                )
                independent_schedule_rows.extend(
                    schedule_evidence_rows(
                        cohort_id=str(cohort_id),
                        group_id=str(group_id),
                        horizon=horizon,
                        block="diagnostic_all",
                        metrics=metrics,
                    )
                )
                for period in full_period_rows:
                    period_block = strict_block_id(
                        str(period["entry_date"]),
                        str(period["exit_date"]),
                        blocks,
                    )
                    if period_block is None:
                        excluded_cross_block_period_count += 1
                    period_rows.append(
                        {
                            "cohort_id": str(cohort_id),
                            "group_id": str(group_id),
                            "horizon_sessions": str(horizon),
                            "evaluation_block": (
                                period_block or "EXCLUDED_CROSS_BLOCK_OR_OUTSIDE"
                            ),
                            "asof_date": str(period.get("asof_date") or ""),
                            "entry_date": str(period.get("entry_date") or ""),
                            "exit_date": str(period.get("exit_date") or ""),
                            "cross_section": str(
                                period.get("cross_section") or 0
                            ),
                            "selected": str(period.get("selected") or 0),
                            "selected_tickers_json": json.dumps(
                                period.get("selected_tickers") or [],
                                separators=(",", ":"),
                            ),
                            "bottom_tickers_json": json.dumps(
                                period.get("bottom_tickers") or [],
                                separators=(",", ":"),
                            ),
                            **{
                                field: fmt(period.get(field))
                                for field in (
                                    "ic",
                                    "turnover",
                                    "gross_excess",
                                    "net_excess",
                                    "cohort_excess",
                                    "bottom_excess",
                                    "top_minus_cohort_gross",
                                    "top_minus_cohort_net",
                                    "top_minus_bottom_gross",
                                )
                            },
                            "early_terminal_observation_count": str(
                                period.get(
                                    "early_terminal_observation_count"
                                )
                                or 0
                            ),
                            "terminal_proceeds_policy": (
                                "terminal_proceeds_cash_carry_to_"
                                "benchmark_exit_zero_return"
                            ),
                            "late_security_entry_observation_count": str(
                                period.get(
                                    "late_security_entry_observation_count"
                                )
                                or 0
                            ),
                            "late_security_entry_policy": (
                                "cash_carry_from_benchmark_entry_to_"
                                "security_entry_zero_return"
                            ),
                        }
                    )
                for block in block_ids:
                    block_metrics = summarize_fixed_block(
                        source_rows=group_rows,
                        horizon=horizon,
                        block=block,
                        blocks=blocks,
                        top_fraction=float(
                            policy["aggregation"]["selection_fraction"]
                        ),
                        minimum_cross_section=int(
                            group["minimum_cross_section"]
                        ),
                        transaction_cost_bps=float(
                            gates["transaction_cost_bps"]
                        ),
                    )
                    gated = gate_row(
                        cohort_id=str(cohort_id),
                        group_id=str(group_id),
                        ranking_mode=ranking_mode,
                        horizon=horizon,
                        block=block,
                        metrics=block_metrics,
                        gates=gates,
                    )
                    result_rows.append(gated)
                    independent_schedule_rows.extend(
                        schedule_evidence_rows(
                            cohort_id=str(cohort_id),
                            group_id=str(group_id),
                            horizon=horizon,
                            block=block,
                            metrics=block_metrics,
                        )
                    )
                    if horizon == int(gates["primary_horizon_sessions"]):
                        group_block_gates.append(gated["group_gate"])
            group_summary[key] = {
                "status": "CALIBRATED_REVEALED_DIAGNOSTIC",
                "all_fixed_blocks_pass": bool(group_block_gates)
                and all(value == "PASS" for value in group_block_gates),
                "primary_block_gates": group_block_gates,
            }

    cohort_results: dict[str, Any] = {}
    for cohort_id, cohort in policy["cohorts"].items():
        ranked_keys = [
            f"{cohort_id}::{group_id}"
            for group_id, group in cohort["groups"].items()
            if str(group["ranking_mode"]) == "ranked"
        ]
        group_pass = bool(ranked_keys) and all(
            bool(group_summary[key]["all_fixed_blocks_pass"]) for key in ranked_keys
        )
        cohort_results[str(cohort_id)] = {
            "cohort_diagnostic_calibration_authorized": cohort_authorization.get(
                str(cohort_id), False
            ),
            "ranked_group_count": len(ranked_keys),
            "all_ranked_groups_pass": group_pass,
            "aggregate_calibration_run": False,
            "aggregate_calibration_reason": (
                "AUTHORIZED_BUT_NOT_PRODUCTION_ELIGIBLE"
                if group_pass
                else "NOT_RUN_GROUP_GATES_FAILED_OR_COHORT_NOT_AUTHORIZED"
            ),
        }

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result_csv = output_dir / "transportation_v8_group_calibration_results.csv"
    period_csv = output_dir / "transportation_v8_group_period_results.csv"
    schedule_csv = (
        output_dir / "transportation_v8_independent_schedule_evidence.csv"
    )
    invalid_interval_csv = (
        output_dir / "transportation_v8_invalid_interval_evidence.csv"
    )
    output_path = output_dir / "transportation_v8_subgroup_calibration.json"
    if not args.allow_overwrite:
        existing = [
            str(path)
            for path in (
                result_csv,
                period_csv,
                schedule_csv,
                invalid_interval_csv,
                output_path,
            )
            if path.exists()
        ]
        if existing:
            raise FileExistsError(
                "v8 diagnostic calibration artifacts are sealed; "
                f"choose a new --output-dir or use --allow-overwrite: {existing}"
            )
    write_csv_atomic(result_csv, RESULT_FIELDS, result_rows)
    write_csv_atomic(period_csv, PERIOD_FIELDS, period_rows)
    write_csv_atomic(
        schedule_csv,
        SCHEDULE_FIELDS,
        independent_schedule_rows,
    )
    write_csv_atomic(
        invalid_interval_csv,
        INVALID_INTERVAL_FIELDS,
        invalid_interval_rows,
    )
    payload = {
        **calibration_truth_labels(group_summary),
        "contract_version": (
            "transportation_v8_subgroup_diagnostic_calibration_v3"
        ),
        "asof_date": args.asof,
        "decision": "NO_PRODUCTION_PROMOTION_FROM_REVEALED_V8_HISTORY",
        "group_results": group_summary,
        "cohort_results": cohort_results,
        "lineage": {
            name: {"path": str(path), "sha256": file_sha256(path)}
            for name, path in paths.items()
        },
        "artifacts": {
            "group_results": {"path": str(result_csv), "sha256": file_sha256(result_csv)},
            "period_results": {"path": str(period_csv), "sha256": file_sha256(period_csv)},
            "independent_schedule_evidence": {
                "path": str(schedule_csv),
                "sha256": file_sha256(schedule_csv),
            },
            "invalid_interval_evidence": {
                "path": str(invalid_interval_csv),
                "sha256": file_sha256(invalid_interval_csv),
            },
        },
        "historical_score_regeneration_count": 1,
        "historical_calibration_execution_count": 1,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "fixed_block_gate_semantics": (
            "all_primary_horizon_blocks_pass_using_only_independent_"
            "statistics_and_benchmark_execution_interval_contained"
        ),
        "overlapping_statistics_role": "DESCRIPTIVE_ONLY_NOT_GATED",
        "independent_schedule_selection_policy": (
            "outcome_blind_greedy_nonoverlap_on_unique_common_"
            "benchmark_entry_date_and_benchmark_exit_date"
        ),
        "turnover_state_policy": (
            "recomputed_on_each_independent_schedule_initial_long_"
            "sleeve_turnover_equals_one"
        ),
        "terminal_proceeds_policy": (
            "terminal_proceeds_cash_carry_to_benchmark_exit_zero_return"
        ),
        "early_terminal_observation_count": (
            early_terminal_observation_count
        ),
        "late_security_entry_observation_count": (
            late_security_entry_observation_count
        ),
        "late_security_entry_policy": (
            "cash_carry_from_benchmark_entry_to_security_entry_zero_return"
        ),
        "invalid_benchmark_interval_cross_section_count": (
            invalid_benchmark_interval_cross_section_count
        ),
        "right_censored_interval_cross_section_count": sum(
            row["right_censored_at_panel_end_flag"] == "1"
            for row in invalid_interval_rows
        ),
        "non_censoring_interval_contract_failure_count": sum(
            row["right_censored_at_panel_end_flag"] != "1"
            for row in invalid_interval_rows
        ),
        "invalid_benchmark_interval_policy": (
            "missing_or_nonunique_common_benchmark_interval_and_"
            "outcome_contract_fail_closed"
        ),
        "excluded_cross_block_period_count": excluded_cross_block_period_count,
        "join_identity_policy": (
            "unique_score_and_panel_keys_plus_exact_source_score_sha256"
        ),
        "next_gate": "FREEZE_V8_DIAGNOSTIC_AND_BEGIN_FUTURE_ONLY_GROUP_MONITORING",
    }
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
