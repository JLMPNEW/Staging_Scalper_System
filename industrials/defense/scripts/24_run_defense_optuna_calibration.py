#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.metric_contract import STRUCTURALLY_DISABLED_PILLARS  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    MODEL_FAMILY,
    PILLAR_SCORE_FIELDS,
    as_float,
    command_line,
    normalize_weights,
    random_weights,
    read_csv_rows,
    sha256_file,
    spearman,
    utc_now,
    weighted_score,
    write_json_atomic,
)


TRIAL_FIELDS = [
    "trial_number",
    "search_method",
    "search_metric",
    "objective_value",
    "train_ic",
    "validation_ic",
    "holdout_ic",
    "train_top_quantile_excess",
    "validation_top_quantile_excess",
    "holdout_top_quantile_excess",
    "train_top_bottom_spread",
    "validation_top_bottom_spread",
    "holdout_top_bottom_spread",
    "train_portfolio_periods",
    "validation_portfolio_periods",
    "holdout_portfolio_periods",
    "train_ic_periods",
    "validation_ic_periods",
    "holdout_ic_periods",
    "train_ic_stdev",
    "validation_ic_stdev",
    "holdout_ic_stdev",
    "train_icir",
    "validation_icir",
    "holdout_icir",
    "train_pooled_ic",
    "validation_pooled_ic",
    "holdout_pooled_ic",
    "train_rows",
    "validation_rows",
    "holdout_rows",
    *[f"weight_{field}" for field in PILLAR_SCORE_FIELDS],
    "proposal_weights_json",
    "weights_json",
]
SUMMARY_FIELDS = [
    "status",
    "search_method",
    "objective",
    "panel_rows",
    "eligible_rows",
    "train_rows",
    "validation_rows",
    "holdout_rows",
    "trial_count",
    "search_metric",
    "selection_metric",
    "best_trial_number",
    "train_ic",
    "validation_ic",
    "holdout_ic",
    "train_top_quantile_excess",
    "validation_top_quantile_excess",
    "holdout_top_quantile_excess",
    "train_top_bottom_spread",
    "validation_top_bottom_spread",
    "holdout_top_bottom_spread",
    "train_portfolio_periods",
    "validation_portfolio_periods",
    "holdout_portfolio_periods",
    "top_quantile",
    "min_positions",
    "train_ic_periods",
    "validation_ic_periods",
    "holdout_ic_periods",
    "train_ic_stdev",
    "validation_ic_stdev",
    "holdout_ic_stdev",
    "train_icir",
    "validation_icir",
    "holdout_icir",
    "train_pooled_ic",
    "validation_pooled_ic",
    "holdout_pooled_ic",
    "promotable",
    "reason",
    "best_weights_json",
    "fixed_zero_pillars",
    "inactive_pillars",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run report-only constrained defense score calibration.")
    default_panel = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_panel" / "defense_oos_calibration_panel.csv"
    default_output = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "optuna_calibration"
    parser.add_argument("--panel-csv", type=Path, default=default_panel)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--objective", default="forward_excess_return_vs_sector")
    parser.add_argument("--trials", type=int, default=64)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--sampler",
        choices=["tpe", "random"],
        default="tpe",
        help="Use random for matched experiments so baseline and candidate receive identical trial weights.",
    )
    parser.add_argument(
        "--selection-metric",
        choices=["validation_ic", "validation_top_quantile_excess"],
        default="validation_ic",
        help="Metric used to select the final trial. Holdout is never used for selection.",
    )
    parser.add_argument("--top-quantile", type=float, default=0.20)
    parser.add_argument("--min-positions", type=int, default=5)
    parser.add_argument("--min-train-rows", type=int, default=100)
    parser.add_argument("--min-holdout-rows", type=int, default=50)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def finite_rows(rows: list[dict[str, str]], *, split_name: str, objective: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("panel_row_eligible_flag") or "") != "1":
            continue
        if str(row.get("split_name") or "") != split_name:
            continue
        if as_float(row.get(objective)) is None:
            continue
        if all(as_float(row.get(field)) is None for field in PILLAR_SCORE_FIELDS) and as_float(row.get("final_score")) is None:
            continue
        out.append(row)
    return out


@dataclass(frozen=True)
class ICStats:
    mean_ic: float | None
    period_count: int
    stdev_ic: float | None
    icir: float | None
    pooled_ic: float | None


@dataclass(frozen=True)
class PortfolioStats:
    mean_top_quantile_excess: float | None
    mean_top_bottom_spread: float | None
    period_count: int


def information_coefficient(rows: list[dict[str, str]], weights: dict[str, float], *, objective: str) -> float | None:
    return information_coefficient_stats(rows, weights, objective=objective).mean_ic


def information_coefficient_stats(
    rows: list[dict[str, str]],
    weights: dict[str, float],
    *,
    objective: str,
) -> ICStats:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    scores: list[float] = []
    outcomes: list[float] = []
    for row in rows:
        score = weighted_score(row, weights)
        outcome = as_float(row.get(objective))
        if score is None or outcome is None:
            continue
        scores.append(score)
        outcomes.append(outcome)
        grouped[str(row.get("asof_date") or "")].append((score, outcome))
    period_ics: list[float] = []
    for pairs in grouped.values():
        if len(pairs) < 3:
            continue
        period_ic = spearman(
            [score for score, _ in pairs],
            [outcome for _, outcome in pairs],
        )
        if period_ic is not None and math.isfinite(period_ic):
            period_ics.append(period_ic)
    mean_ic = sum(period_ics) / len(period_ics) if period_ics else None
    if len(period_ics) >= 2 and mean_ic is not None:
        variance = sum((value - mean_ic) ** 2 for value in period_ics) / (len(period_ics) - 1)
        stdev_ic = math.sqrt(variance)
    else:
        stdev_ic = None
    icir = mean_ic / stdev_ic if mean_ic is not None and stdev_ic is not None and stdev_ic != 0.0 else None
    return ICStats(
        mean_ic=mean_ic,
        period_count=len(period_ics),
        stdev_ic=stdev_ic,
        icir=icir,
        pooled_ic=spearman(scores, outcomes),
    )


def portfolio_stats(
    rows: list[dict[str, str]],
    weights: dict[str, float],
    *,
    objective: str,
    top_quantile: float,
    min_positions: int,
) -> PortfolioStats:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        score = weighted_score(row, weights)
        outcome = as_float(row.get(objective))
        if score is None or outcome is None:
            continue
        grouped[str(row.get("asof_date") or "")].append((score, outcome))
    top_excess: list[float] = []
    top_bottom_spreads: list[float] = []
    for pairs in grouped.values():
        pairs.sort(key=lambda item: item[0], reverse=True)
        if not pairs:
            continue
        selected_count = min(
            len(pairs),
            max(min_positions, int(math.ceil(len(pairs) * top_quantile))),
        )
        top = pairs[:selected_count]
        top_mean = sum(outcome for _, outcome in top) / len(top)
        top_excess.append(top_mean)
        if len(pairs) >= selected_count * 2:
            bottom = pairs[-selected_count:]
            bottom_mean = sum(outcome for _, outcome in bottom) / len(bottom)
            top_bottom_spreads.append(top_mean - bottom_mean)
    return PortfolioStats(
        mean_top_quantile_excess=(
            sum(top_excess) / len(top_excess) if top_excess else None
        ),
        mean_top_bottom_spread=(
            sum(top_bottom_spreads) / len(top_bottom_spreads)
            if top_bottom_spreads
            else None
        ),
        period_count=len(top_excess),
    )


def search_objective_value(
    rows: list[dict[str, str]],
    weights: dict[str, float],
    *,
    objective: str,
    search_metric: str,
    top_quantile: float,
    min_positions: int,
) -> float:
    if search_metric == "train_ic":
        value = information_coefficient(rows, weights, objective=objective)
    elif search_metric == "train_top_quantile_excess":
        value = portfolio_stats(
            rows,
            weights,
            objective=objective,
            top_quantile=top_quantile,
            min_positions=min_positions,
        ).mean_top_quantile_excess
    else:
        raise ValueError(f"Unsupported search metric: {search_metric}")
    if value is None or not math.isfinite(value):
        return -999.0
    return value


def evaluate_trial(
    *,
    trial_number: int,
    search_method: str,
    weights: dict[str, float],
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
    search_metric: str,
    top_quantile: float,
    min_positions: int,
    inactive_pillars: frozenset[str] = frozenset(),
) -> dict[str, str]:
    proposal = normalize_weights(weights)
    normalized = constrain_trial_weights(
        weights,
        inactive_pillars=inactive_pillars,
    )
    train_stats = information_coefficient_stats(train_rows, normalized, objective=objective)
    validation_stats = information_coefficient_stats(validation_rows, normalized, objective=objective)
    holdout_stats = information_coefficient_stats(holdout_rows, normalized, objective=objective)
    train_ic = train_stats.mean_ic
    validation_ic = validation_stats.mean_ic
    holdout_ic = holdout_stats.mean_ic
    train_portfolio = portfolio_stats(
        train_rows,
        normalized,
        objective=objective,
        top_quantile=top_quantile,
        min_positions=min_positions,
    )
    validation_portfolio = portfolio_stats(
        validation_rows,
        normalized,
        objective=objective,
        top_quantile=top_quantile,
        min_positions=min_positions,
    )
    holdout_portfolio = portfolio_stats(
        holdout_rows,
        normalized,
        objective=objective,
        top_quantile=top_quantile,
        min_positions=min_positions,
    )
    search_value = (
        train_ic
        if search_metric == "train_ic"
        else train_portfolio.mean_top_quantile_excess
    )
    record = {
        "trial_number": str(trial_number),
        "search_method": search_method,
        "search_metric": search_metric,
        "objective_value": "" if search_value is None else f"{search_value:.10f}",
        "train_ic": "" if train_ic is None else f"{train_ic:.10f}",
        "validation_ic": "" if validation_ic is None else f"{validation_ic:.10f}",
        "holdout_ic": "" if holdout_ic is None else f"{holdout_ic:.10f}",
        "train_rows": str(len(train_rows)),
        "validation_rows": str(len(validation_rows)),
        "holdout_rows": str(len(holdout_rows)),
        "proposal_weights_json": json.dumps(proposal, sort_keys=True),
        "weights_json": json.dumps(normalized, sort_keys=True),
    }
    for name, stats in [
        ("train", train_stats),
        ("validation", validation_stats),
        ("holdout", holdout_stats),
    ]:
        record[f"{name}_ic_periods"] = str(stats.period_count)
        record[f"{name}_ic_stdev"] = "" if stats.stdev_ic is None else f"{stats.stdev_ic:.10f}"
        record[f"{name}_icir"] = "" if stats.icir is None else f"{stats.icir:.10f}"
        record[f"{name}_pooled_ic"] = "" if stats.pooled_ic is None else f"{stats.pooled_ic:.10f}"
    for name, stats in [
        ("train", train_portfolio),
        ("validation", validation_portfolio),
        ("holdout", holdout_portfolio),
    ]:
        record[f"{name}_top_quantile_excess"] = (
            ""
            if stats.mean_top_quantile_excess is None
            else f"{stats.mean_top_quantile_excess:.10f}"
        )
        record[f"{name}_top_bottom_spread"] = (
            ""
            if stats.mean_top_bottom_spread is None
            else f"{stats.mean_top_bottom_spread:.10f}"
        )
        record[f"{name}_portfolio_periods"] = str(stats.period_count)
    for field in PILLAR_SCORE_FIELDS:
        record[f"weight_{field}"] = f"{normalized[field]:.10f}"
    return record


def inactive_pillars_for_calibration(
    rows: list[dict[str, str]],
) -> frozenset[str]:
    inactive = set(STRUCTURALLY_DISABLED_PILLARS)
    for field in PILLAR_SCORE_FIELDS:
        values = {
            value
            for row in rows
            if (value := as_float(row.get(field))) is not None
        }
        if len(values) <= 1:
            inactive.add(field)
    return frozenset(inactive)


def constrain_trial_weights(
    weights: dict[str, float],
    *,
    inactive_pillars: frozenset[str] = frozenset(),
) -> dict[str, float]:
    constrained = dict(weights)
    for field in STRUCTURALLY_DISABLED_PILLARS | inactive_pillars:
        constrained[field] = 0.0
    return normalize_weights(constrained)


def run_optuna_search(
    *,
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
    trials: int,
    seed: int,
    sampler_name: str,
    search_metric: str,
    top_quantile: float,
    min_positions: int,
    inactive_pillars: frozenset[str],
) -> tuple[str, list[dict[str, str]]]:
    optuna = importlib.import_module("optuna")
    if sampler_name == "random":
        sampler = optuna.samplers.RandomSampler(seed=seed)
        method = "optuna_random"
    else:
        sampler = optuna.samplers.TPESampler(seed=seed)
        method = "optuna_tpe"
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def optuna_objective(trial: Any) -> float:
        weights = {
            field: float(trial.suggest_float(field, 0.0, 1.0))
            for field in PILLAR_SCORE_FIELDS
        }
        return search_objective_value(
            train_rows,
            constrain_trial_weights(
                weights,
                inactive_pillars=inactive_pillars,
            ),
            objective=objective,
            search_metric=search_metric,
            top_quantile=top_quantile,
            min_positions=min_positions,
        )

    study.optimize(optuna_objective, n_trials=trials, show_progress_bar=False)
    records: list[dict[str, str]] = []
    for trial in study.trials:
        weights = {
            field: float(trial.params.get(field, 0.0))
            for field in PILLAR_SCORE_FIELDS
        }
        records.append(
            evaluate_trial(
                trial_number=int(trial.number),
                search_method=method,
                weights=weights,
                train_rows=train_rows,
                validation_rows=validation_rows,
                holdout_rows=holdout_rows,
                objective=objective,
                search_metric=search_metric,
                top_quantile=top_quantile,
                min_positions=min_positions,
                inactive_pillars=inactive_pillars,
            )
        )
    return method, records


def run_fallback_search(
    *,
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
    trials: int,
    seed: int,
    search_metric: str,
    top_quantile: float,
    min_positions: int,
    inactive_pillars: frozenset[str],
) -> tuple[str, list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    for idx, weights in enumerate(random_weights(seed, trials)):
        records.append(
            evaluate_trial(
                trial_number=idx,
                search_method="deterministic_random_search",
                weights=weights,
                train_rows=train_rows,
                validation_rows=validation_rows,
                holdout_rows=holdout_rows,
                objective=objective,
                search_metric=search_metric,
                top_quantile=top_quantile,
                min_positions=min_positions,
                inactive_pillars=inactive_pillars,
            )
        )
    return "deterministic_random_search", records


def select_best_trial(
    records: list[dict[str, str]],
    *,
    selection_metric: str,
) -> tuple[dict[str, str] | None, str]:
    """Select on validation only; holdout remains untouched.

    Portfolio-aligned selection also requires positive validation IC.
    """
    best: dict[str, str] | None = None
    best_value = -math.inf
    for record in records:
        value = as_float(record.get(selection_metric))
        validation_ic = as_float(record.get("validation_ic"))
        if value is None:
            continue
        if (
            selection_metric == "validation_top_quantile_excess"
            and (validation_ic is None or validation_ic <= 0.0)
        ):
            continue
        if value > best_value:
            best = record
            best_value = value
    return (best, selection_metric) if best is not None else (None, "none")


def valid_existing(output_dir: Path) -> bool:
    trials = output_dir / "defense_optuna_calibration_trials.csv"
    summary = output_dir / "defense_optuna_calibration_summary.csv"
    manifest = output_dir / "defense_optuna_calibration_manifest.json"
    if not trials.exists() or not summary.exists() or not manifest.exists():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    files = payload.get("files")
    if not isinstance(files, dict):
        return False
    for path in [trials, summary]:
        meta = files.get(path.name)
        if not isinstance(meta, dict) or meta.get("sha256") != sha256_file(path):
            return False
    return True


def write_empty_outputs(
    *,
    output_dir: Path,
    panel_csv: Path,
    objective: str,
    rows: list[dict[str, str]],
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    search_metric: str,
    selection_metric: str,
    top_quantile: float,
    min_positions: int,
    reason: str,
    inactive_pillars: frozenset[str],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    trials_path = output_dir / "defense_optuna_calibration_trials.csv"
    summary_path = output_dir / "defense_optuna_calibration_summary.csv"
    eligible_rows = sum(1 for row in rows if str(row.get("panel_row_eligible_flag") or "") == "1")
    summary_row = {
        "status": "insufficient_data",
        "search_method": "not_run",
        "objective": objective,
        "panel_rows": len(rows),
        "eligible_rows": eligible_rows,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "holdout_rows": len(holdout_rows),
        "trial_count": 0,
        "search_metric": search_metric,
        "selection_metric": selection_metric,
        "best_trial_number": "",
        "train_ic": "",
        "validation_ic": "",
        "holdout_ic": "",
        "train_top_quantile_excess": "",
        "validation_top_quantile_excess": "",
        "holdout_top_quantile_excess": "",
        "train_top_bottom_spread": "",
        "validation_top_bottom_spread": "",
        "holdout_top_bottom_spread": "",
        "train_portfolio_periods": "0",
        "validation_portfolio_periods": "0",
        "holdout_portfolio_periods": "0",
        "top_quantile": f"{top_quantile:.10f}",
        "min_positions": str(min_positions),
        "train_ic_periods": "0",
        "validation_ic_periods": "0",
        "holdout_ic_periods": "0",
        "train_ic_stdev": "",
        "validation_ic_stdev": "",
        "holdout_ic_stdev": "",
        "train_icir": "",
        "validation_icir": "",
        "holdout_icir": "",
        "train_pooled_ic": "",
        "validation_pooled_ic": "",
        "holdout_pooled_ic": "",
        "promotable": "0",
        "reason": reason,
        "best_weights_json": "",
        "fixed_zero_pillars": ";".join(sorted(STRUCTURALLY_DISABLED_PILLARS)),
        "inactive_pillars": ";".join(sorted(inactive_pillars)),
    }
    write_csv_atomic(trials_path, TRIAL_FIELDS, [])
    write_csv_atomic(summary_path, SUMMARY_FIELDS, [summary_row])
    write_manifest(
        output_dir=output_dir,
        panel_csv=panel_csv,
        trials_path=trials_path,
        summary_path=summary_path,
        summary_row=summary_row,
        best_weights={},
        inactive_pillars=inactive_pillars,
    )


def write_manifest(
    *,
    output_dir: Path,
    panel_csv: Path,
    trials_path: Path,
    summary_path: Path,
    summary_row: dict[str, Any],
    best_weights: dict[str, float],
    inactive_pillars: frozenset[str],
) -> None:
    manifest_path = output_dir / "defense_optuna_calibration_manifest.json"
    manifest = {
        "artifact_family": "defense_optuna_calibration",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "generator": "24_run_defense_optuna_calibration.py",
        "command": command_line(),
        "panel_csv": str(panel_csv),
        "panel_sha256": sha256_file(panel_csv),
        "status": summary_row.get("status"),
        "objective": summary_row.get("objective"),
        "search_method": summary_row.get("search_method", ""),
        "search_metric": summary_row.get("search_metric", ""),
        "selection_metric": summary_row.get("selection_metric", ""),
        "top_quantile": summary_row.get("top_quantile", ""),
        "min_positions": summary_row.get("min_positions", ""),
        "promotable": False,
        "promotion_blockers": ["report_only_shadow_calibration", "manual_review_required"],
        "best_weights": best_weights,
        "fixed_zero_pillars": sorted(STRUCTURALLY_DISABLED_PILLARS),
        "inactive_pillars": sorted(inactive_pillars),
        "files": {
            trials_path.name: {"path": str(trials_path), "sha256": sha256_file(trials_path)},
            summary_path.name: {"path": str(summary_path), "sha256": sha256_file(summary_path)},
        },
    }
    write_json_atomic(manifest_path, manifest)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.trials <= 0:
        raise ValueError("--trials must be positive")
    if args.min_train_rows < 0 or args.min_holdout_rows < 0:
        raise ValueError("Minimum row counts cannot be negative")
    if not 0.0 < args.top_quantile <= 1.0:
        raise ValueError("--top-quantile must be > 0 and <= 1")
    if args.min_positions <= 0:
        raise ValueError("--min-positions must be positive")
    search_metric = (
        "train_top_quantile_excess"
        if args.selection_metric == "validation_top_quantile_excess"
        else "train_ic"
    )
    panel_csv = args.panel_csv.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not panel_csv.exists():
        raise FileNotFoundError(panel_csv)
    artifact_names = [
        "defense_optuna_calibration_trials.csv",
        "defense_optuna_calibration_summary.csv",
        "defense_optuna_calibration_manifest.json",
    ]
    # Guard on the artifact files, not bare directory existence: an empty
    # directory left by a crashed run must not block a rebuild.
    if any((output_dir / name).exists() for name in artifact_names) and not args.allow_overwrite:
        if valid_existing(output_dir):
            print(f"Existing sealed calibration artifacts are valid; keeping {output_dir}")
            return 0
        raise FileExistsError(f"Refusing to overwrite existing calibration artifacts under {output_dir}; use --allow-overwrite")
    rows = read_csv_rows(panel_csv)
    train_rows = finite_rows(rows, split_name="train", objective=args.objective)
    validation_rows = finite_rows(rows, split_name="validation", objective=args.objective)
    holdout_rows = finite_rows(rows, split_name="holdout", objective=args.objective)
    inactive_pillars = inactive_pillars_for_calibration(
        [*train_rows, *validation_rows]
    )
    if len(train_rows) < args.min_train_rows or len(holdout_rows) < args.min_holdout_rows:
        reason = (
            f"insufficient eligible train/holdout rows: train={len(train_rows)}/{args.min_train_rows} "
            f"holdout={len(holdout_rows)}/{args.min_holdout_rows}"
        )
        write_empty_outputs(
            output_dir=output_dir,
            panel_csv=panel_csv,
            objective=args.objective,
            rows=rows,
            train_rows=train_rows,
            validation_rows=validation_rows,
            holdout_rows=holdout_rows,
            search_metric=search_metric,
            selection_metric=args.selection_metric,
            top_quantile=args.top_quantile,
            min_positions=args.min_positions,
            reason=reason,
            inactive_pillars=inactive_pillars,
        )
        print(f"Calibration not run: {reason}")
        print(f"Wrote {output_dir}")
        return 0

    try:
        search_method, trial_rows = run_optuna_search(
            train_rows=train_rows,
            validation_rows=validation_rows,
            holdout_rows=holdout_rows,
            objective=args.objective,
            trials=args.trials,
            seed=args.seed,
            sampler_name=args.sampler,
            search_metric=search_metric,
            top_quantile=args.top_quantile,
            min_positions=args.min_positions,
            inactive_pillars=inactive_pillars,
        )
    except ImportError:
        search_method, trial_rows = run_fallback_search(
            train_rows=train_rows,
            validation_rows=validation_rows,
            holdout_rows=holdout_rows,
            objective=args.objective,
            trials=args.trials,
            seed=args.seed,
            search_metric=search_metric,
            top_quantile=args.top_quantile,
            min_positions=args.min_positions,
            inactive_pillars=inactive_pillars,
        )
    best, selection_metric = select_best_trial(
        trial_rows,
        selection_metric=args.selection_metric,
    )
    if best is None:
        reason = "all trials produced null objective"
        write_empty_outputs(
            output_dir=output_dir,
            panel_csv=panel_csv,
            objective=args.objective,
            rows=rows,
            train_rows=train_rows,
            validation_rows=validation_rows,
            holdout_rows=holdout_rows,
            search_metric=search_metric,
            selection_metric=args.selection_metric,
            top_quantile=args.top_quantile,
            min_positions=args.min_positions,
            reason=reason,
            inactive_pillars=inactive_pillars,
        )
        print(f"Calibration not promoted: {reason}")
        return 0
    best_weights = constrain_trial_weights(
        json.loads(best["weights_json"]),
        inactive_pillars=inactive_pillars,
    )
    summary_row = {
        "status": "report_only_calibration_complete",
        "search_method": search_method,
        "objective": args.objective,
        "panel_rows": len(rows),
        "eligible_rows": sum(1 for row in rows if str(row.get("panel_row_eligible_flag") or "") == "1"),
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "holdout_rows": len(holdout_rows),
        "trial_count": len(trial_rows),
        "search_metric": search_metric,
        "selection_metric": selection_metric,
        "best_trial_number": best["trial_number"],
        "train_ic": best["train_ic"],
        "validation_ic": best["validation_ic"],
        "holdout_ic": best["holdout_ic"],
        "train_top_quantile_excess": best["train_top_quantile_excess"],
        "validation_top_quantile_excess": best["validation_top_quantile_excess"],
        "holdout_top_quantile_excess": best["holdout_top_quantile_excess"],
        "train_top_bottom_spread": best["train_top_bottom_spread"],
        "validation_top_bottom_spread": best["validation_top_bottom_spread"],
        "holdout_top_bottom_spread": best["holdout_top_bottom_spread"],
        "train_portfolio_periods": best["train_portfolio_periods"],
        "validation_portfolio_periods": best["validation_portfolio_periods"],
        "holdout_portfolio_periods": best["holdout_portfolio_periods"],
        "top_quantile": f"{args.top_quantile:.10f}",
        "min_positions": str(args.min_positions),
        "train_ic_periods": best["train_ic_periods"],
        "validation_ic_periods": best["validation_ic_periods"],
        "holdout_ic_periods": best["holdout_ic_periods"],
        "train_ic_stdev": best["train_ic_stdev"],
        "validation_ic_stdev": best["validation_ic_stdev"],
        "holdout_ic_stdev": best["holdout_ic_stdev"],
        "train_icir": best["train_icir"],
        "validation_icir": best["validation_icir"],
        "holdout_icir": best["holdout_icir"],
        "train_pooled_ic": best["train_pooled_ic"],
        "validation_pooled_ic": best["validation_pooled_ic"],
        "holdout_pooled_ic": best["holdout_pooled_ic"],
        "promotable": "0",
        "reason": "report_only_shadow_calibration_requires_manual_review_and_validated_pit_oos_panel",
        "best_weights_json": json.dumps(best_weights, sort_keys=True),
        "fixed_zero_pillars": ";".join(sorted(STRUCTURALLY_DISABLED_PILLARS)),
        "inactive_pillars": ";".join(sorted(inactive_pillars)),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    trials_path = output_dir / "defense_optuna_calibration_trials.csv"
    summary_path = output_dir / "defense_optuna_calibration_summary.csv"
    write_csv_atomic(trials_path, TRIAL_FIELDS, trial_rows)
    write_csv_atomic(summary_path, SUMMARY_FIELDS, [summary_row])
    write_manifest(
        output_dir=output_dir,
        panel_csv=panel_csv,
        trials_path=trials_path,
        summary_path=summary_path,
        summary_row=summary_row,
        best_weights=best_weights,
        inactive_pillars=inactive_pillars,
    )
    print(
        f"Calibration report complete: method={search_method} trials={len(trial_rows)} "
        f"train_ic={summary_row['train_ic']} holdout_ic={summary_row['holdout_ic']}"
    )
    print(f"Wrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
