#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    load_metric_registry,
)
from industrials.transportation.oos_outcomes import (  # noqa: E402
    finite_float,
    fmt,
    write_gzip_csv_atomic,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    iter_gzip_csv,
    read_json,
    sha256,
    verify_artifact,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.walk_forward_calibration import (  # noqa: E402
    CALIBRATION_VERSION,
    aggregate_period_rows,
    equal_weights,
    generic_baseline_scores,
    overlay_score,
    percentile_scores,
    ranked_sleeves,
    spearman,
    turnover,
)


EXPECTED_CANDIDATES = (
    "fleet_utilization",
    "operating_ratio",
    "passenger_load_factor",
)
EXPECTED_BENCHMARKS = ("IYT", "XTN", "SPY")
EXPECTED_WEIGHTS = (0.0, 0.025, 0.05, 0.075, 0.10)
CALIBRATION_EPSILON = 1e-12

OBSERVATION_FIELDS = (
    "asof_date",
    "ticker",
    "calibration_cohort",
    "industry",
    "universe_role",
    "metric_id",
    "split_name",
    "metric_value",
    "direction_adjusted_metric_value",
    "specialized_percentile",
    "baseline_score",
    "baseline_component_count",
    "baseline_generic_metric_count",
    "security_forward_return",
    "forward_excess_return_vs_IYT",
    "forward_excess_return_vs_XTN",
    "forward_excess_return_vs_SPY",
    "source_panel_eligible_flag",
    "calibration_input_eligible_flag",
    "calibration_input_reason",
)
PERIOD_FIELDS = (
    "metric_id",
    "calibration_cohort",
    "weight",
    "benchmark",
    "split_name",
    "asof_date",
    "cross_section_count",
    "rank_ic",
    "top_count",
    "bottom_count",
    "top_tickers",
    "bottom_tickers",
    "top_mean_excess_return",
    "bottom_mean_excess_return",
    "gross_top_bottom_spread",
    "top_one_way_turnover",
    "bottom_one_way_turnover",
    "top_traded_notional",
    "bottom_traded_notional",
    "base_transaction_cost",
    "stress_transaction_cost",
    "net_top_bottom_spread_base",
    "net_top_bottom_spread_stress",
)
GRID_FIELDS = (
    "metric_id",
    "calibration_cohort",
    "weight",
    "benchmark",
    "split_name",
    "period_count",
    "row_count",
    "mean_rank_ic",
    "mean_top_excess_return",
    "mean_bottom_excess_return",
    "mean_gross_top_bottom_spread",
    "average_top_one_way_turnover",
    "average_bottom_one_way_turnover",
    "mean_base_transaction_cost",
    "mean_stress_transaction_cost",
    "mean_net_top_bottom_spread_base",
    "mean_net_top_bottom_spread_stress",
)
SELECTION_FIELDS = (
    "metric_id",
    "calibration_cohort",
    "validation_selected_weight",
    "validation_candidate_pass",
    "selection_rank_ic_lift",
    "selection_net_spread_lift",
    "holdout_evaluated_weight",
    "holdout_period_minimum",
    "holdout_rank_ic_gate_pass",
    "holdout_net_spread_gate_pass",
    "holdout_turnover_gate_pass",
    "holdout_all_benchmarks_gate_pass",
    "final_research_weight",
    "decision",
    "decision_reason",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the single bounded transportation specialized-metric "
            "walk-forward research calibration against hash-frozen inputs."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(
            "output/industrials/transportation/historical_features/"
            "v3_conflict_resolved"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def _artifact(path: Path, *, row_count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": sha256(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def _metric_cohorts(contract: Mapping[str, Any]) -> dict[str, str]:
    overlay = contract.get("cohort_specific_overlay") or {}
    return {
        str(metric): str(cohort)
        for cohort, metric in overlay.items()
        if metric
    }


def _as_float(value: object, *, label: str) -> float:
    parsed = finite_float(value)
    if parsed is None:
        raise ValueError(f"{label}: expected finite numeric value")
    return parsed


def _as_int(value: object, *, label: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: expected integer") from error


def _mean(values: Iterable[float | None]) -> float | None:
    members = [float(value) for value in values if value is not None]
    return mean(members) if members else None


def _existing_valid(
    *,
    manifest_path: Path,
    output_paths: Mapping[str, Path],
    input_hashes: Mapping[str, str],
    generator_hash: str,
    module_hash: str,
) -> bool:
    if not manifest_path.is_file() or any(
        not path.is_file() for path in output_paths.values()
    ):
        return False
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if (
        manifest.get("acceptance") != "PASS"
        or manifest.get("calibration_executed") is not True
        or (manifest.get("operations") or {}).get(
            "calibration_invocations"
        )
        != 1
        or str((manifest.get("generator") or {}).get("script_sha256") or "")
        != generator_hash
        or str(
            (manifest.get("generator") or {}).get(
                "calibration_module_sha256"
            )
            or ""
        )
        != module_hash
    ):
        return False
    recorded_inputs = manifest.get("input_hashes") or {}
    if {
        str(key): str(value) for key, value in recorded_inputs.items()
    } != dict(input_hashes):
        return False
    artifacts = manifest.get("artifacts") or {}
    for label, path in output_paths.items():
        reference = artifacts.get(label) or {}
        if str(reference.get("sha256") or "") != sha256(path):
            return False
    return True


def _build_observations(
    *,
    outcome_rows: Sequence[Mapping[str, str]],
    baselines: Mapping[tuple[str, str], Mapping[str, float]],
    metric_cohorts: Mapping[str, str],
) -> list[dict[str, object]]:
    adjusted_by_group: dict[tuple[str, str], dict[str, float]] = defaultdict(
        dict
    )
    for row in outcome_rows:
        metric = str(row.get("metric_id") or "")
        cohort = str(row.get("calibration_cohort") or "")
        if metric_cohorts.get(metric) != cohort:
            continue
        value = finite_float(row.get("direction_adjusted_metric_value"))
        if value is not None:
            adjusted_by_group[
                (str(row.get("asof_date") or ""), metric)
            ][str(row.get("ticker") or "")] = value
    percentiles = {
        group: percentile_scores(values)
        for group, values in adjusted_by_group.items()
    }

    output: list[dict[str, object]] = []
    for row in outcome_rows:
        metric = str(row.get("metric_id") or "")
        cohort = str(row.get("calibration_cohort") or "")
        if metric_cohorts.get(metric) != cohort:
            continue
        asof = str(row.get("asof_date") or "")
        ticker = str(row.get("ticker") or "")
        baseline = baselines.get((asof, ticker))
        specialized = percentiles.get((asof, metric), {}).get(ticker)
        reasons: list[str] = []
        if row.get("panel_row_eligible_flag") != "1":
            reasons.append(
                str(row.get("panel_row_eligible_reason") or "panel_ineligible")
            )
        if baseline is None:
            reasons.append("missing_generic_baseline")
        if specialized is None:
            reasons.append("missing_specialized_percentile")
        eligible = not reasons
        output.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "calibration_cohort": cohort,
                "industry": str(row.get("industry") or ""),
                "universe_role": str(row.get("universe_role") or ""),
                "metric_id": metric,
                "split_name": str(row.get("split_name") or ""),
                "metric_value": str(row.get("metric_value") or ""),
                "direction_adjusted_metric_value": str(
                    row.get("direction_adjusted_metric_value") or ""
                ),
                "specialized_percentile": fmt(specialized),
                "baseline_score": fmt(
                    baseline.get("baseline_score") if baseline else None
                ),
                "baseline_component_count": (
                    int(baseline["baseline_component_count"])
                    if baseline
                    else ""
                ),
                "baseline_generic_metric_count": (
                    int(baseline["baseline_generic_metric_count"])
                    if baseline
                    else ""
                ),
                "security_forward_return": str(
                    row.get("security_forward_return") or ""
                ),
                "forward_excess_return_vs_IYT": str(
                    row.get("forward_excess_return_vs_IYT") or ""
                ),
                "forward_excess_return_vs_XTN": str(
                    row.get("forward_excess_return_vs_XTN") or ""
                ),
                "forward_excess_return_vs_SPY": str(
                    row.get("forward_excess_return_vs_SPY") or ""
                ),
                "source_panel_eligible_flag": int(
                    row.get("panel_row_eligible_flag") == "1"
                ),
                "calibration_input_eligible_flag": int(eligible),
                "calibration_input_reason": (
                    "eligible" if eligible else ";".join(reasons)
                ),
            }
        )
    output.sort(
        key=lambda row: (
            str(row["metric_id"]),
            str(row["asof_date"]),
            str(row["ticker"]),
        )
    )
    return output


def _period_rows(
    observations: Sequence[Mapping[str, object]],
    *,
    metric: str,
    cohort: str,
    weights: Sequence[float],
    splits: Sequence[str],
    benchmarks: Sequence[str],
    base_cost_rate: float,
    stress_cost_rate: float,
    minimum_tickers: int,
) -> list[dict[str, object]]:
    by_split_date: dict[tuple[str, str], list[Mapping[str, object]]] = (
        defaultdict(list)
    )
    for row in observations:
        if (
            str(row.get("metric_id") or "") == metric
            and str(row.get("calibration_cohort") or "") == cohort
            and str(row.get("calibration_input_eligible_flag") or "") == "1"
        ):
            by_split_date[
                (
                    str(row.get("split_name") or ""),
                    str(row.get("asof_date") or ""),
                )
            ].append(row)

    output: list[dict[str, object]] = []
    for split in splits:
        for weight in weights:
            previous_top: dict[str, float] | None = None
            previous_bottom: dict[str, float] | None = None
            for asof in sorted(
                date
                for candidate_split, date in by_split_date
                if candidate_split == split
            ):
                members = by_split_date[(split, asof)]
                scored: list[tuple[Mapping[str, object], float]] = []
                for row in members:
                    baseline = finite_float(row.get("baseline_score"))
                    specialized = finite_float(
                        row.get("specialized_percentile")
                    )
                    if baseline is None or specialized is None:
                        continue
                    scored.append(
                        (
                            row,
                            overlay_score(baseline, specialized, weight),
                        )
                    )
                if len(scored) < minimum_tickers:
                    continue
                sleeve = ranked_sleeves(scored)
                if sleeve is None:
                    continue
                top_weights = equal_weights(sleeve.top)
                bottom_weights = equal_weights(sleeve.bottom)
                top_one_way, top_traded = turnover(
                    top_weights,
                    previous_top,
                )
                bottom_one_way, bottom_traded = turnover(
                    bottom_weights,
                    previous_bottom,
                )
                previous_top = top_weights
                previous_bottom = bottom_weights
                base_cost = (top_traded + bottom_traded) * base_cost_rate
                stress_cost = (
                    top_traded + bottom_traded
                ) * stress_cost_rate
                top_tickers = "|".join(
                    str(row.get("ticker") or "") for row, _ in sleeve.top
                )
                bottom_tickers = "|".join(
                    str(row.get("ticker") or "") for row, _ in sleeve.bottom
                )
                for benchmark in benchmarks:
                    field = f"forward_excess_return_vs_{benchmark}"
                    rank_scores: list[float] = []
                    rank_outcomes: list[float] = []
                    for row, score in scored:
                        outcome = finite_float(row.get(field))
                        if outcome is not None:
                            rank_scores.append(score)
                            rank_outcomes.append(outcome)
                    top_values = [
                        _as_float(row.get(field), label=f"{metric}:{field}")
                        for row, _ in sleeve.top
                    ]
                    bottom_values = [
                        _as_float(row.get(field), label=f"{metric}:{field}")
                        for row, _ in sleeve.bottom
                    ]
                    top_mean = mean(top_values)
                    bottom_mean = mean(bottom_values)
                    gross = top_mean - bottom_mean
                    output.append(
                        {
                            "metric_id": metric,
                            "calibration_cohort": cohort,
                            "weight": fmt(weight),
                            "benchmark": benchmark,
                            "split_name": split,
                            "asof_date": asof,
                            "cross_section_count": len(scored),
                            "rank_ic": fmt(
                                spearman(rank_scores, rank_outcomes)
                            ),
                            "top_count": len(sleeve.top),
                            "bottom_count": len(sleeve.bottom),
                            "top_tickers": top_tickers,
                            "bottom_tickers": bottom_tickers,
                            "top_mean_excess_return": fmt(top_mean),
                            "bottom_mean_excess_return": fmt(bottom_mean),
                            "gross_top_bottom_spread": fmt(gross),
                            "top_one_way_turnover": fmt(top_one_way),
                            "bottom_one_way_turnover": fmt(
                                bottom_one_way
                            ),
                            "top_traded_notional": fmt(top_traded),
                            "bottom_traded_notional": fmt(bottom_traded),
                            "base_transaction_cost": fmt(base_cost),
                            "stress_transaction_cost": fmt(stress_cost),
                            "net_top_bottom_spread_base": fmt(
                                gross - base_cost
                            ),
                            "net_top_bottom_spread_stress": fmt(
                                gross - stress_cost
                            ),
                        }
                    )
    return output


def _aggregate_grid(
    period_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[
        tuple[str, str, str, str, str],
        list[Mapping[str, object]],
    ] = defaultdict(list)
    for row in period_rows:
        grouped[
            (
                str(row["metric_id"]),
                str(row["calibration_cohort"]),
                str(row["weight"]),
                str(row["benchmark"]),
                str(row["split_name"]),
            )
        ].append(row)
    output: list[dict[str, object]] = []
    for key, members in sorted(grouped.items()):
        summary = aggregate_period_rows(members)
        output.append(
            {
                "metric_id": key[0],
                "calibration_cohort": key[1],
                "weight": key[2],
                "benchmark": key[3],
                "split_name": key[4],
                **{
                    field: (
                        fmt(value)
                        if isinstance(value, float)
                        else value if value is not None else ""
                    )
                    for field, value in summary.items()
                },
            }
        )
    return output


def _grid_index(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, float, str, str], Mapping[str, object]]:
    return {
        (
            str(row["metric_id"]),
            _as_float(row["weight"], label="grid.weight"),
            str(row["benchmark"]),
            str(row["split_name"]),
        ): row
        for row in rows
    }


def _value(row: Mapping[str, object], field: str) -> float | None:
    return finite_float(row.get(field))


def _validation_pass(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, object],
    ],
    *,
    metric: str,
    weight: float,
    benchmarks: Sequence[str],
    turnover_max: float,
) -> bool:
    if weight <= 0:
        return False
    for benchmark in benchmarks:
        candidate = index.get((metric, weight, benchmark, "validation"))
        baseline = index.get((metric, 0.0, benchmark, "validation"))
        if candidate is None or baseline is None:
            return False
        candidate_ic = _value(candidate, "mean_rank_ic")
        baseline_ic = _value(baseline, "mean_rank_ic")
        candidate_base = _value(
            candidate,
            "mean_net_top_bottom_spread_base",
        )
        baseline_base = _value(
            baseline,
            "mean_net_top_bottom_spread_base",
        )
        candidate_stress = _value(
            candidate,
            "mean_net_top_bottom_spread_stress",
        )
        baseline_stress = _value(
            baseline,
            "mean_net_top_bottom_spread_stress",
        )
        top_turnover = _value(
            candidate,
            "average_top_one_way_turnover",
        )
        bottom_turnover = _value(
            candidate,
            "average_bottom_one_way_turnover",
        )
        if (
            candidate_ic is None
            or baseline_ic is None
            or candidate_base is None
            or baseline_base is None
            or candidate_stress is None
            or baseline_stress is None
            or top_turnover is None
            or bottom_turnover is None
            or candidate_ic < 0
            or candidate_base < 0
            or candidate_stress < 0
            or max(top_turnover, bottom_turnover) > turnover_max
            or candidate_ic <= baseline_ic + CALIBRATION_EPSILON
            or candidate_base <= baseline_base + CALIBRATION_EPSILON
            or candidate_stress <= baseline_stress + CALIBRATION_EPSILON
        ):
            return False
    return True


def _selection_lifts(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, object],
    ],
    *,
    metric: str,
    weight: float,
    benchmarks: Sequence[str],
) -> tuple[float, float]:
    ic_lifts: list[float] = []
    spread_lifts: list[float] = []
    for split in ("train", "validation"):
        for benchmark in benchmarks:
            # A split can legitimately produce no period rows (every date
            # below the minimum cross-section), so missing grid cells are
            # skipped instead of crashing the bounded run.
            candidate = index.get((metric, weight, benchmark, split))
            baseline = index.get((metric, 0.0, benchmark, split))
            if candidate is None or baseline is None:
                continue
            candidate_ic = _value(candidate, "mean_rank_ic")
            baseline_ic = _value(baseline, "mean_rank_ic")
            candidate_spread = _value(
                candidate,
                "mean_net_top_bottom_spread_stress",
            )
            baseline_spread = _value(
                baseline,
                "mean_net_top_bottom_spread_stress",
            )
            if candidate_ic is not None and baseline_ic is not None:
                ic_lifts.append(candidate_ic - baseline_ic)
            if (
                candidate_spread is not None
                and baseline_spread is not None
            ):
                spread_lifts.append(candidate_spread - baseline_spread)
    return mean(ic_lifts), mean(spread_lifts)


def _select_validation_weights(
    grid_rows: Sequence[Mapping[str, object]],
    *,
    metric_cohorts: Mapping[str, str],
    benchmarks: Sequence[str],
    weights: Sequence[float],
    turnover_max: float,
) -> dict[str, dict[str, Any]]:
    index = _grid_index(grid_rows)
    output: dict[str, dict[str, Any]] = {}
    for metric, cohort in sorted(metric_cohorts.items()):
        passing: list[tuple[float, float, float]] = []
        for weight in weights:
            if not _validation_pass(
                index,
                metric=metric,
                weight=weight,
                benchmarks=benchmarks,
                turnover_max=turnover_max,
            ):
                continue
            ic_lift, spread_lift = _selection_lifts(
                index,
                metric=metric,
                weight=weight,
                benchmarks=benchmarks,
            )
            passing.append((weight, ic_lift, spread_lift))
        if passing:
            selected, ic_lift, spread_lift = max(
                passing,
                key=lambda item: (item[1], item[2], -item[0]),
            )
        else:
            selected, ic_lift, spread_lift = 0.0, 0.0, 0.0
        output[metric] = {
            "metric_id": metric,
            "calibration_cohort": cohort,
            "validation_selected_weight": selected,
            "validation_candidate_pass": bool(passing),
            "selection_rank_ic_lift": ic_lift,
            "selection_net_spread_lift": spread_lift,
        }
    return output


def _holdout_decisions(
    selection: Mapping[str, Mapping[str, Any]],
    grid_rows: Sequence[Mapping[str, object]],
    *,
    benchmarks: Sequence[str],
    minimum_periods: int,
    turnover_max: float,
) -> list[dict[str, object]]:
    index = _grid_index(grid_rows)
    output: list[dict[str, object]] = []
    for metric, item in sorted(selection.items()):
        selected = float(item["validation_selected_weight"])
        rank_pass = selected > 0
        spread_pass = selected > 0
        turnover_pass = selected > 0
        periods_pass = selected > 0
        if selected > 0:
            for benchmark in benchmarks:
                row = index.get((metric, selected, benchmark, "holdout"))
                if row is None:
                    rank_pass = spread_pass = turnover_pass = False
                    periods_pass = False
                    continue
                periods_pass = periods_pass and (
                    _as_int(row["period_count"], label="holdout.period_count")
                    >= minimum_periods
                )
                rank_ic = _value(row, "mean_rank_ic")
                base = _value(row, "mean_net_top_bottom_spread_base")
                stress = _value(
                    row,
                    "mean_net_top_bottom_spread_stress",
                )
                top_turnover = _value(
                    row,
                    "average_top_one_way_turnover",
                )
                bottom_turnover = _value(
                    row,
                    "average_bottom_one_way_turnover",
                )
                rank_pass = rank_pass and rank_ic is not None and rank_ic >= 0
                spread_pass = (
                    spread_pass
                    and base is not None
                    and stress is not None
                    and base >= 0
                    and stress >= 0
                )
                turnover_pass = (
                    turnover_pass
                    and top_turnover is not None
                    and bottom_turnover is not None
                    and max(top_turnover, bottom_turnover) <= turnover_max
                )
        all_pass = (
            selected > 0
            and periods_pass
            and rank_pass
            and spread_pass
            and turnover_pass
        )
        final_weight = selected if all_pass else 0.0
        if selected <= 0:
            decision = "RETAIN_ZERO_OVERLAY"
            reason = "no_nonzero_weight_passed_validation_selection_gates"
        elif all_pass:
            decision = "CONFIRM_RESEARCH_OVERLAY"
            reason = "selected_weight_passed_all_holdout_confirmation_gates"
        else:
            decision = "RETAIN_ZERO_OVERLAY"
            reason = "selected_weight_failed_holdout_confirmation"
        output.append(
            {
                **item,
                "validation_selected_weight": fmt(selected),
                "validation_candidate_pass": int(
                    bool(item["validation_candidate_pass"])
                ),
                "selection_rank_ic_lift": fmt(
                    float(item["selection_rank_ic_lift"])
                ),
                "selection_net_spread_lift": fmt(
                    float(item["selection_net_spread_lift"])
                ),
                "holdout_evaluated_weight": fmt(selected),
                "holdout_period_minimum": int(periods_pass),
                "holdout_rank_ic_gate_pass": int(rank_pass),
                "holdout_net_spread_gate_pass": int(spread_pass),
                "holdout_turnover_gate_pass": int(turnover_pass),
                "holdout_all_benchmarks_gate_pass": int(all_pass),
                "final_research_weight": fmt(final_weight),
                "decision": decision,
                "decision_reason": reason,
            }
        )
    return output


def _git_source_control(*paths: Path) -> dict[str, Any]:
    relative_paths = [
        path.resolve().relative_to(PROJECT_ROOT).as_posix()
        for path in paths
    ]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for relative_path in relative_paths:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative_path],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *relative_paths],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise ValueError(
            "calibration source paths must be committed before execution="
            f"{dirty.splitlines()}"
        )
    return {
        "git_commit_sha": head,
        "tracked_paths": relative_paths,
        "worktree_clean_for_paths": True,
    }

def main() -> int:
    args = parse_args()
    generator_path = Path(__file__).resolve()
    module_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "walk_forward_calibration.py"
    ).resolve()
    config_path = args.config.expanduser().resolve()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_dir
    )
    contract_path = (
        input_dir / "transportation_walk_forward_calibration_contract.json"
    )
    readiness_path = (
        input_dir / "transportation_walk_forward_outcome_validation.json"
    )
    outcome_manifest_path = (
        input_dir
        / "transportation_walk_forward_outcome_panel_manifest.json"
    )
    panel_manifest_path = (
        input_dir / "transportation_v3_panel_manifest.json"
    )
    for path in (
        contract_path,
        readiness_path,
        outcome_manifest_path,
        panel_manifest_path,
        config_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    contract = read_json(contract_path)
    readiness = read_json(readiness_path)
    outcome_manifest = read_json(outcome_manifest_path)
    panel_manifest = read_json(panel_manifest_path)
    if (
        contract.get("acceptance") != "PASS"
        or contract.get("single_calibration_authorized") is not True
        or readiness.get("acceptance") != "PASS"
        or readiness.get("single_calibration_authorized") is not True
        or readiness.get("ready_candidate_metric_count") != 3
        or outcome_manifest.get("acceptance") != "PASS"
        or panel_manifest.get("acceptance") != "PASS"
        or panel_manifest.get("panel_status") != "HASH_FROZEN"
    ):
        raise ValueError("DP9, DP10, DP11, and DP12 must pass and authorize")
    candidates = tuple(str(value) for value in contract["candidate_metric_ids"])
    if candidates != EXPECTED_CANDIDATES:
        raise ValueError(f"candidate contract changed={candidates}")
    if tuple(readiness["ready_candidate_metric_ids"]) != EXPECTED_CANDIDATES:
        raise ValueError("readiness candidate order or set changed")
    metric_cohorts = _metric_cohorts(contract)
    if set(metric_cohorts) != set(EXPECTED_CANDIDATES):
        raise ValueError("cohort-specific overlay mapping changed")
    optimization = contract.get("optimization_contract") or {}
    weights = tuple(float(value) for value in optimization["candidate_weights"])
    if weights != EXPECTED_WEIGHTS:
        raise ValueError(f"candidate weight grid changed={weights}")
    outcome_contract = contract.get("outcome_contract") or {}
    benchmarks = (
        str(outcome_contract["primary_benchmark"]),
        *tuple(
            str(value)
            for value in outcome_contract["robustness_benchmarks"]
        ),
    )
    if benchmarks != EXPECTED_BENCHMARKS:
        raise ValueError(f"benchmark contract changed={benchmarks}")
    gates = contract.get("acceptance_gates") or {}
    minimum_tickers = int(gates["minimum_candidate_value_tickers"])
    minimum_holdout = int(
        gates["minimum_holdout_periods_per_candidate_cohort"]
    )
    turnover_max = float(gates["maximum_average_one_way_turnover"])
    base_cost_rate = (
        float(
            outcome_contract[
                "transaction_cost_bps_per_one_way_turnover"
            ]
        )
        / 10000.0
    )
    stress_cost_rate = (
        float(outcome_contract["transaction_cost_stress_bps"]) / 10000.0
    )

    complete_path = verify_artifact(
        (panel_manifest.get("artifacts") or {}).get("complete_panel") or {},
        label="DP9 complete panel",
    )
    outcome_path = verify_artifact(
        (readiness.get("artifacts") or {}).get("outcome_panel") or {},
        label="DP12 outcome panel",
    )
    if sha256(complete_path) != str(contract.get("panel_sha256") or ""):
        raise ValueError("DP9 complete-panel hash differs from DP10 contract")
    if sha256(outcome_path) != str(
        (
            (outcome_manifest.get("artifacts") or {}).get(
                "outcome_panel"
            )
            or {}
        ).get("sha256")
        or ""
    ):
        raise ValueError("DP11 and DP12 outcome-panel hashes differ")

    config = load_yaml(config_path)
    family = cfg_get(config, "model_families.transportation", {}) or {}
    registry_path = resolve_path(
        (family.get("financial") or {})["metric_registry"],
        base_dir=config_path.parent,
    )
    registry_version, definitions = load_metric_registry(registry_path)
    generic_definitions = [
        definition for definition in definitions if not definition.specialized
    ]
    if len(generic_definitions) != 18:
        raise ValueError(
            f"frozen generic definition count changed={len(generic_definitions)}"
        )
    component_weights = {
        str(key): float(value)
        for key, value in (
            (family.get("scoring") or {}).get("component_weights") or {}
        ).items()
    }

    output_paths = {
        "observations": (
            output_dir
            / "transportation_walk_forward_calibration_observations.csv.gz"
        ),
        "period_results": (
            output_dir / "transportation_walk_forward_calibration_periods.csv"
        ),
        "grid_summary": (
            output_dir / "transportation_walk_forward_calibration_grid.csv"
        ),
        "selection": (
            output_dir
            / "transportation_walk_forward_calibration_selection.csv"
        ),
    }
    manifest_path = (
        output_dir
        / "transportation_walk_forward_calibration_manifest.json"
    )
    input_hashes = {
        "config": sha256(config_path),
        "metric_registry": sha256(registry_path),
        "complete_panel": sha256(complete_path),
        "panel_manifest": sha256(panel_manifest_path),
        "calibration_contract": sha256(contract_path),
        "outcome_panel": sha256(outcome_path),
        "outcome_manifest": sha256(outcome_manifest_path),
        "outcome_readiness": sha256(readiness_path),
    }
    generator_hash = sha256(generator_path)
    module_hash = sha256(module_path)
    source_control = _git_source_control(
        generator_path,
        module_path,
        config_path,
        registry_path,
    )
    if not args.allow_overwrite and _existing_valid(
        manifest_path=manifest_path,
        output_paths=output_paths,
        input_hashes=input_hashes,
        generator_hash=generator_hash,
        module_hash=module_hash,
    ):
        print(manifest_path.read_text(encoding="utf-8"), end="")
        return 0
    if not args.allow_overwrite and (
        manifest_path.exists()
        or any(path.exists() for path in output_paths.values())
    ):
        raise FileExistsError(
            "calibration artifacts exist but do not match frozen inputs; "
            "manual review is required before --allow-overwrite"
        )

    generic_rows = [
        row
        for row in iter_gzip_csv(complete_path)
        if row.get("metric_family") == "generic"
        and row.get("source_lane") == "V2_GENERIC"
    ]
    baselines = generic_baseline_scores(
        generic_rows,
        definitions=generic_definitions,
        component_weights=component_weights,
    )
    outcome_rows = list(iter_gzip_csv(outcome_path))
    observations = _build_observations(
        outcome_rows=outcome_rows,
        baselines=baselines,
        metric_cohorts=metric_cohorts,
    )

    train_validation_periods: list[dict[str, object]] = []
    for metric, cohort in sorted(metric_cohorts.items()):
        train_validation_periods.extend(
            _period_rows(
                observations,
                metric=metric,
                cohort=cohort,
                weights=weights,
                splits=("train", "validation"),
                benchmarks=benchmarks,
                base_cost_rate=base_cost_rate,
                stress_cost_rate=stress_cost_rate,
                minimum_tickers=minimum_tickers,
            )
        )
    train_validation_grid = _aggregate_grid(train_validation_periods)
    selected = _select_validation_weights(
        train_validation_grid,
        metric_cohorts=metric_cohorts,
        benchmarks=benchmarks,
        weights=weights,
        turnover_max=turnover_max,
    )

    holdout_periods: list[dict[str, object]] = []
    holdout_weights: dict[str, list[float]] = {}
    for metric, cohort in sorted(metric_cohorts.items()):
        selected_weight = float(
            selected[metric]["validation_selected_weight"]
        )
        evaluated = sorted({0.0, selected_weight})
        holdout_weights[metric] = evaluated
        holdout_periods.extend(
            _period_rows(
                observations,
                metric=metric,
                cohort=cohort,
                weights=evaluated,
                splits=("holdout",),
                benchmarks=benchmarks,
                base_cost_rate=base_cost_rate,
                stress_cost_rate=stress_cost_rate,
                minimum_tickers=minimum_tickers,
            )
        )
    period_rows = sorted(
        [*train_validation_periods, *holdout_periods],
        key=lambda row: (
            str(row["metric_id"]),
            str(row["split_name"]),
            float(str(row["weight"])),
            str(row["benchmark"]),
            str(row["asof_date"]),
        ),
    )
    grid_rows = _aggregate_grid(period_rows)
    selection_rows = _holdout_decisions(
        selected,
        grid_rows,
        benchmarks=benchmarks,
        minimum_periods=minimum_holdout,
        turnover_max=turnover_max,
    )

    observation_count = write_gzip_csv_atomic(
        output_paths["observations"],
        OBSERVATION_FIELDS,
        observations,
    )
    write_csv_atomic(
        output_paths["period_results"],
        PERIOD_FIELDS,
        period_rows,
    )
    write_csv_atomic(output_paths["grid_summary"], GRID_FIELDS, grid_rows)
    write_csv_atomic(
        output_paths["selection"],
        SELECTION_FIELDS,
        selection_rows,
    )
    confirmed = [
        str(row["metric_id"])
        for row in selection_rows
        if finite_float(row["final_research_weight"]) not in {None, 0.0}
    ]
    payload: dict[str, Any] = {
        "acceptance": "PASS",
        "gate": "DP13_RUN_SINGLE_BOUNDED_WALK_FORWARD_CALIBRATION",
        "calibration_version": CALIBRATION_VERSION,
        "model_family": MODEL_FAMILY,
        "registry_version": registry_version,
        "candidate_metric_ids": list(candidates),
        "candidate_weight_grid": list(weights),
        "benchmark_tickers": list(benchmarks),
        "candidate_decisions": {
            str(row["metric_id"]): {
                "calibration_cohort": str(row["calibration_cohort"]),
                "validation_selected_weight": float(
                    str(row["validation_selected_weight"])
                ),
                "final_research_weight": float(
                    str(row["final_research_weight"])
                ),
                "decision": str(row["decision"]),
                "decision_reason": str(row["decision_reason"]),
            }
            for row in selection_rows
        },
        "confirmed_research_metric_ids": confirmed,
        "confirmed_research_metric_count": len(confirmed),
        "observation_row_count": observation_count,
        "eligible_observation_row_count": sum(
            str(row["calibration_input_eligible_flag"]) == "1"
            for row in observations
        ),
        "period_result_row_count": len(period_rows),
        "grid_summary_row_count": len(grid_rows),
        "selection_row_count": len(selection_rows),
        "holdout_used_for_selection": False,
        "holdout_weights_evaluated_by_metric": holdout_weights,
        "research_only": True,
        "portfolio_rebalance_policy_defined": False,
        "production_promotion_authorized": False,
        "portfolio_shadow_validation_executed": False,
        "artifacts": {
            label: _artifact(
                path,
                row_count={
                    "observations": observation_count,
                    "period_results": len(period_rows),
                    "grid_summary": len(grid_rows),
                    "selection": len(selection_rows),
                }[label],
            )
            for label, path in output_paths.items()
        },
        "inputs": {
            "config": _artifact(config_path),
            "metric_registry": _artifact(registry_path),
            "complete_panel": _artifact(
                complete_path,
                row_count=len(generic_rows),
            ),
            "panel_manifest": _artifact(panel_manifest_path),
            "calibration_contract": _artifact(contract_path),
            "outcome_panel": _artifact(
                outcome_path,
                row_count=len(outcome_rows),
            ),
            "outcome_manifest": _artifact(outcome_manifest_path),
            "outcome_readiness": _artifact(readiness_path),
        },
        "input_hashes": input_hashes,
        "generator": {
            "script_path": str(generator_path),
            "script_sha256": generator_hash,
            "calibration_module_path": str(module_path),
            "calibration_module_sha256": module_hash,
        },
        "source_control": source_control,
        "operations": {
            "calibration_invocations": 1,
            "database_writes": 0,
            "parser_invocations": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "calibration_executed": True,
        "errors": [],
        "next_gate": (
            "VALIDATE_BOUNDED_WALK_FORWARD_CALIBRATION"
        ),
    }
    write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
