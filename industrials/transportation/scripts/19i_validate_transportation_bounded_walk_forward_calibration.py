#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.oos_outcomes import finite_float  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    iter_gzip_csv,
    read_csv,
    read_json,
    sha256,
    verify_artifact,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    MODEL_FAMILY,
)
from industrials.transportation.walk_forward_calibration import (  # noqa: E402
    CALIBRATION_VERSION,
)


EXPECTED_CANDIDATES = (
    "fleet_utilization",
    "operating_ratio",
    "passenger_load_factor",
)
EXPECTED_BENCHMARKS = ("IYT", "XTN", "SPY")
EXPECTED_WEIGHTS = (0.0, 0.025, 0.05, 0.075, 0.10)
EPSILON = 1e-9

NUMERIC_GRID_FIELDS = (
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Independently validate the single bounded transportation "
            "walk-forward calibration and its holdout boundary."
        )
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(
            "output/industrials/transportation/historical_features/"
            "v3_conflict_resolved"
        ),
    )
    return parser.parse_args()


def _number(value: object, *, required: bool = True) -> float | None:
    parsed = finite_float(value)
    if parsed is None and required:
        raise ValueError(f"expected finite number, received={value!r}")
    return parsed


def _integer(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"expected integer, received={value!r}") from error


def _close(left: float, right: float, *, tolerance: float = EPSILON) -> bool:
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def _metric_cohorts(contract: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(metric): str(cohort)
        for cohort, metric in (
            contract.get("cohort_specific_overlay") or {}
        ).items()
        if metric
    }


def _grid_key(
    row: Mapping[str, str],
) -> tuple[str, float, str, str]:
    return (
        row["metric_id"],
        float(row["weight"]),
        row["benchmark"],
        row["split_name"],
    )


def _mean_field(
    rows: Sequence[Mapping[str, str]],
    field: str,
) -> float | None:
    members = [
        value
        for row in rows
        if (value := finite_float(row.get(field))) is not None
    ]
    return mean(members) if members else None


def _independent_aggregate(
    rows: Sequence[Mapping[str, str]],
) -> dict[str, float | int | None]:
    return {
        "period_count": len(rows),
        "row_count": sum(_integer(row["cross_section_count"]) for row in rows),
        "mean_rank_ic": _mean_field(rows, "rank_ic"),
        "mean_top_excess_return": _mean_field(
            rows,
            "top_mean_excess_return",
        ),
        "mean_bottom_excess_return": _mean_field(
            rows,
            "bottom_mean_excess_return",
        ),
        "mean_gross_top_bottom_spread": _mean_field(
            rows,
            "gross_top_bottom_spread",
        ),
        "average_top_one_way_turnover": _mean_field(
            rows,
            "top_one_way_turnover",
        ),
        "average_bottom_one_way_turnover": _mean_field(
            rows,
            "bottom_one_way_turnover",
        ),
        "mean_base_transaction_cost": _mean_field(
            rows,
            "base_transaction_cost",
        ),
        "mean_stress_transaction_cost": _mean_field(
            rows,
            "stress_transaction_cost",
        ),
        "mean_net_top_bottom_spread_base": _mean_field(
            rows,
            "net_top_bottom_spread_base",
        ),
        "mean_net_top_bottom_spread_stress": _mean_field(
            rows,
            "net_top_bottom_spread_stress",
        ),
    }


def _strictly_better_validation(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, str],
    ],
    *,
    metric: str,
    weight: float,
    turnover_max: float,
) -> bool:
    if weight <= 0:
        return False
    for benchmark in EXPECTED_BENCHMARKS:
        candidate = index.get((metric, weight, benchmark, "validation"))
        baseline = index.get((metric, 0.0, benchmark, "validation"))
        if candidate is None or baseline is None:
            return False
        candidate_ic = _number(candidate["mean_rank_ic"])
        baseline_ic = _number(baseline["mean_rank_ic"])
        candidate_base = _number(
            candidate["mean_net_top_bottom_spread_base"]
        )
        baseline_base = _number(
            baseline["mean_net_top_bottom_spread_base"]
        )
        candidate_stress = _number(
            candidate["mean_net_top_bottom_spread_stress"]
        )
        baseline_stress = _number(
            baseline["mean_net_top_bottom_spread_stress"]
        )
        top_turnover = _number(
            candidate["average_top_one_way_turnover"]
        )
        bottom_turnover = _number(
            candidate["average_bottom_one_way_turnover"]
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
            or candidate_ic <= baseline_ic + 1e-12
            or candidate_base <= baseline_base + 1e-12
            or candidate_stress <= baseline_stress + 1e-12
        ):
            return False
    return True


def _selection_lifts(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, str],
    ],
    *,
    metric: str,
    weight: float,
) -> tuple[float, float]:
    ic_lifts: list[float] = []
    spread_lifts: list[float] = []
    for split in ("train", "validation"):
        for benchmark in EXPECTED_BENCHMARKS:
            # Mirrors the runner: a split with no qualifying period rows has
            # no grid cell, and the lift simply skips that cell.
            candidate = index.get((metric, weight, benchmark, split))
            baseline = index.get((metric, 0.0, benchmark, split))
            if candidate is None or baseline is None:
                continue
            candidate_ic = _number(candidate["mean_rank_ic"])
            baseline_ic = _number(baseline["mean_rank_ic"])
            candidate_spread = _number(
                candidate["mean_net_top_bottom_spread_stress"]
            )
            baseline_spread = _number(
                baseline["mean_net_top_bottom_spread_stress"]
            )
            if candidate_ic is not None and baseline_ic is not None:
                ic_lifts.append(candidate_ic - baseline_ic)
            if (
                candidate_spread is not None
                and baseline_spread is not None
            ):
                spread_lifts.append(candidate_spread - baseline_spread)
    return mean(ic_lifts), mean(spread_lifts)


def _expected_selection(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, str],
    ],
    *,
    metric: str,
    turnover_max: float,
) -> tuple[float, float, float]:
    passing: list[tuple[float, float, float]] = []
    for weight in EXPECTED_WEIGHTS[1:]:
        if _strictly_better_validation(
            index,
            metric=metric,
            weight=weight,
            turnover_max=turnover_max,
        ):
            ic_lift, spread_lift = _selection_lifts(
                index,
                metric=metric,
                weight=weight,
            )
            passing.append((weight, ic_lift, spread_lift))
    if not passing:
        return 0.0, 0.0, 0.0
    return max(
        passing,
        key=lambda item: (item[1], item[2], -item[0]),
    )


def _expected_holdout_pass(
    index: Mapping[
        tuple[str, float, str, str],
        Mapping[str, str],
    ],
    *,
    metric: str,
    selected: float,
    minimum_periods: int,
    turnover_max: float,
) -> tuple[bool, bool, bool, bool]:
    if selected <= 0:
        return False, False, False, False
    periods_pass = True
    rank_pass = True
    spread_pass = True
    turnover_pass = True
    for benchmark in EXPECTED_BENCHMARKS:
        row = index.get((metric, selected, benchmark, "holdout"))
        if row is None:
            return False, False, False, False
        periods_pass = (
            periods_pass and _integer(row["period_count"]) >= minimum_periods
        )
        rank_ic = _number(row["mean_rank_ic"])
        base = _number(row["mean_net_top_bottom_spread_base"])
        stress = _number(row["mean_net_top_bottom_spread_stress"])
        top_turnover = _number(row["average_top_one_way_turnover"])
        bottom_turnover = _number(
            row["average_bottom_one_way_turnover"]
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
    return periods_pass, rank_pass, spread_pass, turnover_pass


def main() -> int:
    args = parse_args()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    manifest_path = (
        artifact_dir
        / "transportation_walk_forward_calibration_manifest.json"
    )
    validation_path = (
        artifact_dir
        / "transportation_walk_forward_calibration_validation.json"
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = read_json(manifest_path)
    errors: list[str] = []
    if (
        manifest.get("acceptance") != "PASS"
        or manifest.get("calibration_version") != CALIBRATION_VERSION
        or manifest.get("model_family") != MODEL_FAMILY
        or manifest.get("calibration_executed") is not True
        or manifest.get("research_only") is not True
        or manifest.get("holdout_used_for_selection") is not False
        or manifest.get("production_promotion_authorized") is not False
    ):
        errors.append("DP13 manifest state or research boundary is invalid")
    if tuple(manifest.get("candidate_metric_ids") or ()) != EXPECTED_CANDIDATES:
        errors.append("candidate metric set or order changed")
    if tuple(manifest.get("benchmark_tickers") or ()) != EXPECTED_BENCHMARKS:
        errors.append("benchmark set or order changed")
    if tuple(
        float(value) for value in manifest.get("candidate_weight_grid") or ()
    ) != EXPECTED_WEIGHTS:
        errors.append("bounded weight grid changed")

    artifacts = manifest.get("artifacts") or {}
    try:
        observations_path = verify_artifact(
            artifacts.get("observations") or {},
            label="calibration observations",
        )
        periods_path = verify_artifact(
            artifacts.get("period_results") or {},
            label="calibration period results",
        )
        grid_path = verify_artifact(
            artifacts.get("grid_summary") or {},
            label="calibration grid summary",
        )
        selection_path = verify_artifact(
            artifacts.get("selection") or {},
            label="calibration selection",
        )
    except (FileNotFoundError, ValueError) as error:
        errors.append(str(error))
        observations_path = periods_path = grid_path = selection_path = (
            artifact_dir / "__missing__"
        )
    for label, reference in (manifest.get("inputs") or {}).items():
        try:
            verify_artifact(reference, label=f"input {label}")
        except (FileNotFoundError, ValueError) as error:
            errors.append(str(error))
    for label, expected_hash in (manifest.get("input_hashes") or {}).items():
        reference = (manifest.get("inputs") or {}).get(label) or {}
        if str(reference.get("sha256") or "") != str(expected_hash):
            errors.append(f"input hash map mismatch={label}")
    generator = manifest.get("generator") or {}
    for path_key, hash_key in (
        ("script_path", "script_sha256"),
        ("calibration_module_path", "calibration_module_sha256"),
    ):
        path = Path(str(generator.get(path_key) or "")).resolve()
        if (
            not path.is_file()
            or sha256(path) != str(generator.get(hash_key) or "")
        ):
            errors.append(f"generator lineage mismatch={path_key}")

    if errors and not observations_path.is_file():
        observations: list[dict[str, str]] = []
        periods: list[dict[str, str]] = []
        grid: list[dict[str, str]] = []
        selections: list[dict[str, str]] = []
    else:
        observations = list(iter_gzip_csv(observations_path))
        periods = read_csv(periods_path)
        grid = read_csv(grid_path)
        selections = read_csv(selection_path)
    expected_counts = {
        "observation_row_count": len(observations),
        "period_result_row_count": len(periods),
        "grid_summary_row_count": len(grid),
        "selection_row_count": len(selections),
    }
    for key, actual in expected_counts.items():
        if _integer(manifest.get(key) or 0) != actual:
            errors.append(f"manifest row-count mismatch={key}")

    contract_reference = (manifest.get("inputs") or {}).get(
        "calibration_contract"
    ) or {}
    contract_path = Path(str(contract_reference.get("path") or "")).resolve()
    contract = read_json(contract_path) if contract_path.is_file() else {}
    metric_cohorts = _metric_cohorts(contract)
    gates = contract.get("acceptance_gates") or {}
    minimum_tickers = int(gates.get("minimum_candidate_value_tickers") or 3)
    minimum_periods = int(
        gates.get("minimum_holdout_periods_per_candidate_cohort") or 12
    )
    turnover_max = float(
        gates.get("maximum_average_one_way_turnover") or 0.75
    )
    outcome_contract = contract.get("outcome_contract") or {}
    base_rate = (
        float(
            outcome_contract.get(
                "transaction_cost_bps_per_one_way_turnover"
            )
            or 20
        )
        / 10000
    )
    stress_rate = (
        float(outcome_contract.get("transaction_cost_stress_bps") or 40)
        / 10000
    )

    observation_keys: set[tuple[str, str, str]] = set()
    for row in observations:
        key = (row["metric_id"], row["asof_date"], row["ticker"])
        if key in observation_keys:
            errors.append(f"duplicate observation={key}")
        observation_keys.add(key)
        metric = row["metric_id"]
        if metric_cohorts.get(metric) != row["calibration_cohort"]:
            errors.append(f"out-of-cohort observation={key}")
        source_eligible = row["source_panel_eligible_flag"] == "1"
        calibration_eligible = (
            row["calibration_input_eligible_flag"] == "1"
        )
        baseline = finite_float(row["baseline_score"])
        specialized = finite_float(row["specialized_percentile"])
        expected_eligible = (
            source_eligible
            and baseline is not None
            and specialized is not None
        )
        if calibration_eligible != expected_eligible:
            errors.append(f"observation eligibility mismatch={key}")
        if calibration_eligible:
            if row["calibration_input_reason"] != "eligible":
                errors.append(f"eligible observation reason mismatch={key}")
            if row["split_name"] == "embargo":
                errors.append(f"embargo observation marked eligible={key}")
            if (
                baseline is None
                or specialized is None
                or not 0 <= baseline <= 100
                or not 0 <= specialized <= 100
            ):
                errors.append(f"eligible score outside 0..100={key}")
            for benchmark in EXPECTED_BENCHMARKS:
                if finite_float(
                    row[f"forward_excess_return_vs_{benchmark}"]
                ) is None:
                    errors.append(f"eligible outcome missing={key}:{benchmark}")

    period_keys: set[tuple[str, str, float, str, str]] = set()
    grouped_periods: dict[
        tuple[str, float, str, str],
        list[dict[str, str]],
    ] = defaultdict(list)
    for row in periods:
        key = (
            row["metric_id"],
            row["split_name"],
            float(row["weight"]),
            row["benchmark"],
            row["asof_date"],
        )
        if key in period_keys:
            errors.append(f"duplicate period result={key}")
        period_keys.add(key)
        grouped_periods[_grid_key(row)].append(row)
        if row["metric_id"] not in EXPECTED_CANDIDATES:
            errors.append(f"unexpected period metric={row['metric_id']}")
        if row["benchmark"] not in EXPECTED_BENCHMARKS:
            errors.append(f"unexpected period benchmark={row['benchmark']}")
        if float(row["weight"]) not in EXPECTED_WEIGHTS:
            errors.append(f"unbounded period weight={row['weight']}")
        if row["split_name"] not in {"train", "validation", "holdout"}:
            errors.append(f"invalid calibrated split={row['split_name']}")
        count = _integer(row["cross_section_count"])
        if count < minimum_tickers:
            errors.append(f"undersized rank period={key}")
        top = [value for value in row["top_tickers"].split("|") if value]
        bottom = [
            value for value in row["bottom_tickers"].split("|") if value
        ]
        if (
            len(top) != _integer(row["top_count"])
            or len(bottom) != _integer(row["bottom_count"])
            or set(top) & set(bottom)
        ):
            errors.append(f"invalid sleeve membership={key}")
        top_mean = _number(row["top_mean_excess_return"])
        bottom_mean = _number(row["bottom_mean_excess_return"])
        gross = _number(row["gross_top_bottom_spread"])
        top_traded = _number(row["top_traded_notional"])
        bottom_traded = _number(row["bottom_traded_notional"])
        base_cost = _number(row["base_transaction_cost"])
        stress_cost = _number(row["stress_transaction_cost"])
        net_base = _number(row["net_top_bottom_spread_base"])
        net_stress = _number(row["net_top_bottom_spread_stress"])
        if (
            top_mean is None
            or bottom_mean is None
            or gross is None
            or top_traded is None
            or bottom_traded is None
            or base_cost is None
            or stress_cost is None
            or net_base is None
            or net_stress is None
            or not _close(gross, top_mean - bottom_mean)
            or not _close(base_cost, (top_traded + bottom_traded) * base_rate)
            or not _close(
                stress_cost,
                (top_traded + bottom_traded) * stress_rate,
            )
            or not _close(net_base, gross - base_cost)
            or not _close(net_stress, gross - stress_cost)
        ):
            errors.append(f"period arithmetic mismatch={key}")
        rank_ic = _number(row["rank_ic"], required=False)
        if rank_ic is not None and not -1 - EPSILON <= rank_ic <= 1 + EPSILON:
            errors.append(f"rank IC outside bounds={key}")

    grid_index: dict[
        tuple[str, float, str, str],
        dict[str, str],
    ] = {}
    for row in grid:
        key = _grid_key(row)
        if key in grid_index:
            errors.append(f"duplicate grid row={key}")
        grid_index[key] = row
        independent = _independent_aggregate(grouped_periods.get(key, []))
        for field in NUMERIC_GRID_FIELDS:
            actual = finite_float(row.get(field))
            expected = independent[field]
            if (
                (actual is None) != (expected is None)
                or (
                    actual is not None
                    and expected is not None
                    and not _close(actual, float(expected))
                )
            ):
                errors.append(f"grid aggregate mismatch={key}:{field}")
    if set(grid_index) != set(grouped_periods):
        errors.append("grid and period-result key sets differ")

    selection_by_metric = {
        row["metric_id"]: row for row in selections
    }
    if (
        len(selection_by_metric) != len(selections)
        or set(selection_by_metric) != set(EXPECTED_CANDIDATES)
    ):
        errors.append("selection rows must contain each candidate exactly once")
    expected_train_validation = {
        (metric, weight, benchmark, split)
        for metric in EXPECTED_CANDIDATES
        for weight in EXPECTED_WEIGHTS
        for benchmark in EXPECTED_BENCHMARKS
        for split in ("train", "validation")
    }
    if not expected_train_validation.issubset(grid_index):
        errors.append("train/validation grid is incomplete")

    selected_weights: dict[str, float] = {}
    final_weights: dict[str, float] = {}
    for metric in EXPECTED_CANDIDATES:
        row = selection_by_metric.get(metric)
        if row is None:
            continue
        expected_weight, expected_ic_lift, expected_spread_lift = (
            _expected_selection(
                grid_index,
                metric=metric,
                turnover_max=turnover_max,
            )
        )
        selected = float(row["validation_selected_weight"])
        final_weight = float(row["final_research_weight"])
        selected_weights[metric] = selected
        final_weights[metric] = final_weight
        if (
            selected != expected_weight
            or not _close(
                float(row["selection_rank_ic_lift"]),
                expected_ic_lift,
            )
            or not _close(
                float(row["selection_net_spread_lift"]),
                expected_spread_lift,
            )
        ):
            errors.append(f"validation selection mismatch={metric}")
        expected_holdout = _expected_holdout_pass(
            grid_index,
            metric=metric,
            selected=selected,
            minimum_periods=minimum_periods,
            turnover_max=turnover_max,
        )
        expected_all = all(expected_holdout)
        reported_holdout = (
            row["holdout_period_minimum"] == "1",
            row["holdout_rank_ic_gate_pass"] == "1",
            row["holdout_net_spread_gate_pass"] == "1",
            row["holdout_turnover_gate_pass"] == "1",
        )
        if reported_holdout != expected_holdout:
            errors.append(f"holdout gate mismatch={metric}")
        if (row["holdout_all_benchmarks_gate_pass"] == "1") != expected_all:
            errors.append(f"holdout composite mismatch={metric}")
        expected_final = selected if expected_all else 0.0
        if final_weight != expected_final:
            errors.append(f"final research weight mismatch={metric}")
        expected_decision = (
            "CONFIRM_RESEARCH_OVERLAY"
            if expected_final > 0
            else "RETAIN_ZERO_OVERLAY"
        )
        if row["decision"] != expected_decision:
            errors.append(f"decision mismatch={metric}")

        actual_holdout_weights = {
            weight
            for candidate_metric, weight, _, split in grid_index
            if candidate_metric == metric and split == "holdout"
        }
        expected_holdout_weights = {0.0, selected}
        if actual_holdout_weights != expected_holdout_weights:
            errors.append(
                f"holdout weight boundary violated={metric}:"
                f"{sorted(actual_holdout_weights)}"
            )
        manifest_weights = {
            float(value)
            for value in (
                manifest.get("holdout_weights_evaluated_by_metric") or {}
            ).get(metric, [])
        }
        if manifest_weights != expected_holdout_weights:
            errors.append(f"manifest holdout weight mismatch={metric}")

    operations = manifest.get("operations") or {}
    expected_operations = {
        "calibration_invocations": 1,
        "database_writes": 0,
        "parser_invocations": 0,
        "network_requests": 0,
        "feature_rebuilds": 0,
        "membership_rebuilds": 0,
        "portfolio_writes": 0,
        "production_config_writes": 0,
    }
    if operations != expected_operations:
        errors.append("operation counters violate bounded calibration contract")
    candidate_decisions = manifest.get("candidate_decisions") or {}
    for metric, selected in selected_weights.items():
        manifest_decision = candidate_decisions.get(metric) or {}
        if (
            float(manifest_decision.get("validation_selected_weight") or 0)
            != selected
            or float(manifest_decision.get("final_research_weight") or 0)
            != final_weights[metric]
        ):
            errors.append(f"manifest candidate decision mismatch={metric}")

    acceptance = "PASS" if not errors else "FAIL"
    confirmed = [
        metric
        for metric, weight in sorted(final_weights.items())
        if weight > 0
    ]
    payload: dict[str, Any] = {
        "acceptance": acceptance,
        "gate": "DP14_VALIDATE_BOUNDED_WALK_FORWARD_CALIBRATION",
        "calibration_version": CALIBRATION_VERSION,
        "model_family": MODEL_FAMILY,
        "candidate_metric_ids": list(EXPECTED_CANDIDATES),
        "validation_selected_weights": selected_weights,
        "final_research_weights": final_weights,
        "confirmed_research_metric_ids": confirmed,
        "confirmed_research_metric_count": len(confirmed),
        "observation_row_count": len(observations),
        "eligible_observation_row_count": sum(
            row["calibration_input_eligible_flag"] == "1"
            for row in observations
        ),
        "period_result_row_count": len(periods),
        "grid_summary_row_count": len(grid),
        "selection_row_count": len(selections),
        "holdout_used_for_selection": False,
        "production_promotion_authorized": False,
        "portfolio_shadow_validation_executed": False,
        "artifacts": {
            "calibration_manifest": {
                "path": str(manifest_path),
                "sha256": sha256(manifest_path),
            },
            "observations": {
                "path": str(observations_path),
                "sha256": (
                    sha256(observations_path)
                    if observations_path.is_file()
                    else ""
                ),
            },
            "period_results": {
                "path": str(periods_path),
                "sha256": sha256(periods_path) if periods_path.is_file() else "",
            },
            "grid_summary": {
                "path": str(grid_path),
                "sha256": sha256(grid_path) if grid_path.is_file() else "",
            },
            "selection": {
                "path": str(selection_path),
                "sha256": (
                    sha256(selection_path) if selection_path.is_file() else ""
                ),
            },
        },
        "operations_verified": expected_operations,
        "errors": errors,
        "next_gate": (
            (
                "BUILD_CALIBRATED_RESEARCH_CANDIDATE_AND_"
                "VALIDATE_PORTFOLIO_SHADOW"
                if confirmed
                else "RETAIN_ZERO_OVERLAYS_AND_VALIDATE_PORTFOLIO_SHADOW"
            )
            if acceptance == "PASS"
            else "REVIEW_BOUNDED_CALIBRATION_VALIDATION_FAILURES"
        ),
    }
    write_text_atomic(
        validation_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
