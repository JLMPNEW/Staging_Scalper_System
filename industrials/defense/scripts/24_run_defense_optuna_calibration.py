#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
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
    "objective_value",
    "train_ic",
    "validation_ic",
    "holdout_ic",
    "train_rows",
    "validation_rows",
    "holdout_rows",
    *[f"weight_{field}" for field in PILLAR_SCORE_FIELDS],
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
    "selection_metric",
    "best_trial_number",
    "train_ic",
    "validation_ic",
    "holdout_ic",
    "promotable",
    "reason",
    "best_weights_json",
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


def information_coefficient(rows: list[dict[str, str]], weights: dict[str, float], *, objective: str) -> float | None:
    scores: list[float] = []
    outcomes: list[float] = []
    for row in rows:
        score = weighted_score(row, weights)
        outcome = as_float(row.get(objective))
        if score is None or outcome is None:
            continue
        scores.append(score)
        outcomes.append(outcome)
    return spearman(scores, outcomes)


def objective_value(rows: list[dict[str, str]], weights: dict[str, float], *, objective: str) -> float:
    ic = information_coefficient(rows, weights, objective=objective)
    if ic is None or not math.isfinite(ic):
        return -999.0
    return ic


def evaluate_trial(
    *,
    trial_number: int,
    search_method: str,
    weights: dict[str, float],
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
) -> dict[str, str]:
    normalized = normalize_weights(weights)
    train_ic = information_coefficient(train_rows, normalized, objective=objective)
    validation_ic = information_coefficient(validation_rows, normalized, objective=objective)
    holdout_ic = information_coefficient(holdout_rows, normalized, objective=objective)
    record = {
        "trial_number": str(trial_number),
        "search_method": search_method,
        "objective_value": "" if train_ic is None else f"{train_ic:.10f}",
        "train_ic": "" if train_ic is None else f"{train_ic:.10f}",
        "validation_ic": "" if validation_ic is None else f"{validation_ic:.10f}",
        "holdout_ic": "" if holdout_ic is None else f"{holdout_ic:.10f}",
        "train_rows": str(len(train_rows)),
        "validation_rows": str(len(validation_rows)),
        "holdout_rows": str(len(holdout_rows)),
        "weights_json": json.dumps(normalized, sort_keys=True),
    }
    for field in PILLAR_SCORE_FIELDS:
        record[f"weight_{field}"] = f"{normalized[field]:.10f}"
    return record


def run_optuna_search(
    *,
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
    trials: int,
    seed: int,
) -> tuple[str, list[dict[str, str]]]:
    optuna = importlib.import_module("optuna")
    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def optuna_objective(trial: Any) -> float:
        weights = {
            field: float(trial.suggest_float(field, 0.0, 1.0))
            for field in PILLAR_SCORE_FIELDS
        }
        return objective_value(train_rows, normalize_weights(weights), objective=objective)

    study.optimize(optuna_objective, n_trials=trials, show_progress_bar=False)
    records: list[dict[str, str]] = []
    for trial in study.trials:
        weights = {field: float(trial.params.get(field, 0.0)) for field in PILLAR_SCORE_FIELDS}
        records.append(
            evaluate_trial(
                trial_number=int(trial.number),
                search_method="optuna_tpe",
                weights=weights,
                train_rows=train_rows,
                validation_rows=validation_rows,
                holdout_rows=holdout_rows,
                objective=objective,
            )
        )
    return "optuna_tpe", records


def run_fallback_search(
    *,
    train_rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    holdout_rows: list[dict[str, str]],
    objective: str,
    trials: int,
    seed: int,
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
            )
        )
    return "deterministic_random_search", records


def select_best_trial(records: list[dict[str, str]]) -> tuple[dict[str, str] | None, str]:
    """Pick the best trial on VALIDATION IC; fall back to train IC only when
    no trial has a validation value.

    Selecting on train IC across many trials just rewards overfit weights —
    the validation split exists to arbitrate selection while holdout stays
    untouched for reporting. Returns (best_record, selection_metric).
    """
    for metric in ("validation_ic", "train_ic"):
        best: dict[str, str] | None = None
        best_value = -math.inf
        for record in records:
            value = as_float(record.get(metric))
            if value is None:
                continue
            if value > best_value:
                best = record
                best_value = value
        if best is not None:
            return best, metric
    return None, "none"


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
    reason: str,
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
        "selection_metric": "none",
        "best_trial_number": "",
        "train_ic": "",
        "validation_ic": "",
        "holdout_ic": "",
        "promotable": "0",
        "reason": reason,
        "best_weights_json": "",
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
    )


def write_manifest(
    *,
    output_dir: Path,
    panel_csv: Path,
    trials_path: Path,
    summary_path: Path,
    summary_row: dict[str, Any],
    best_weights: dict[str, float],
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
        "selection_metric": summary_row.get("selection_metric", ""),
        "promotable": False,
        "promotion_blockers": ["report_only_shadow_calibration", "manual_review_required"],
        "best_weights": best_weights,
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
            reason=reason,
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
        )
    except ImportError:
        search_method, trial_rows = run_fallback_search(
            train_rows=train_rows,
            validation_rows=validation_rows,
            holdout_rows=holdout_rows,
            objective=args.objective,
            trials=args.trials,
            seed=args.seed,
        )
    best, selection_metric = select_best_trial(trial_rows)
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
            reason=reason,
        )
        print(f"Calibration not promoted: {reason}")
        return 0
    best_weights = normalize_weights(json.loads(best["weights_json"]))
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
        "selection_metric": selection_metric,
        "best_trial_number": best["trial_number"],
        "train_ic": best["train_ic"],
        "validation_ic": best["validation_ic"],
        "holdout_ic": best["holdout_ic"],
        "promotable": "0",
        "reason": "report_only_shadow_calibration_requires_manual_review_and_validated_pit_oos_panel",
        "best_weights_json": json.dumps(best_weights, sort_keys=True),
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
    )
    print(
        f"Calibration report complete: method={search_method} trials={len(trial_rows)} "
        f"train_ic={summary_row['train_ic']} holdout_ic={summary_row['holdout_ic']}"
    )
    print(f"Wrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
