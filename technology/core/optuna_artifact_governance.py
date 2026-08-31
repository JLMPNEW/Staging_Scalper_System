"""Cross-family hardening for technology Stage 8 calibration artifacts.

The three sector optimizers intentionally remain independent.  Their thin
wrappers call this module after a native run so governance behavior is uniform:
holdout-only robustness folds, Newey-West significance, immutable artifacts,
and a walk-forward-required final promotion decision.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from technology.core.calibration_governance import (
    final_promotion_decision,
    incumbent_relative_cohort_cap,
    new_run_id,
    seal_calibration_run,
    sha256_file,
    stage8_gate_decision,
    stamp_rows,
    validate_calibration_run_manifest,
    walk_forward_gate_decision,
)
from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.signal_diagnostics import newey_west_lags_for_horizon, newey_west_t_stat, raw_t_stat


LOGGER = logging.getLogger("technology_optuna_artifact_governance")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "technology" / "config.yaml"


@dataclass(frozen=True)
class FamilySpec:
    family: str
    config_key: str
    diagnostics_key: str
    default_output: str
    stage8_manifest: str


FAMILIES: dict[str, FamilySpec] = {
    "semiconductors": FamilySpec(
        family="semiconductors",
        config_key="semiconductor_optuna_calibration",
        diagnostics_key="semiconductor_signal_diagnostics",
        default_output="../output/technology_reports/optuna_calibration",
        stage8_manifest="stage8_run_manifest.json",
    ),
    "software_infrastructure": FamilySpec(
        family="software_infrastructure",
        config_key="software_infrastructure_optuna_calibration",
        diagnostics_key="software_infrastructure_signal_diagnostics",
        default_output="../output/technology_reports/software_infrastructure/optuna_calibration",
        stage8_manifest="stage8_run_manifest.json",
    ),
    "technology_hardware": FamilySpec(
        family="technology_hardware",
        config_key="technology_hardware_optuna_calibration",
        diagnostics_key="technology_hardware_signal_diagnostics",
        default_output="../output/technology_reports/technology_hardware/optuna_calibration",
        stage8_manifest="stage8_run_manifest.json",
    ),
}


def _args_from_argv() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    args, _unknown = parser.parse_known_args(sys.argv[1:])
    return args


def _paths(family: str, config_path: Path, output_dir: Path | None) -> tuple[FamilySpec, dict[str, Any], Path, Path]:
    spec = FAMILIES[family]
    config_path = config_path.expanduser().resolve()
    config = load_yaml(config_path)
    resolved_output = output_dir.expanduser().resolve() if output_dir else resolve_path(
        cfg_get(config, f"{spec.config_key}.output_dir", spec.default_output),
        base_dir=config_path.parent,
    )
    if family == "semiconductors":
        # The semis optimizer constructs its panel in memory. Keep a separate,
        # immutable copy of its full Stage 7 evaluation as panel evidence; the
        # governed output CSVs are stamped later and therefore cannot safely
        # fingerprint themselves.
        panel_path = resolved_output / "stage8_panel_evidence.csv"
    else:
        diagnostics_dir = resolve_path(
            cfg_get(config, f"{spec.config_key}.signal_diagnostics_output_dir", cfg_get(config, f"{spec.diagnostics_key}.output_dir")),
            base_dir=config_path.parent,
        )
        panel_path = diagnostics_dir / "signal_panel.csv"
    return spec, config, resolved_output, panel_path


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError(f"Refusing to write an empty calibration artifact: {path}")
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    temp = path.with_suffix(f"{path.suffix}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temp = path.with_suffix(f"{path.suffix}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _mean(values: Iterable[float]) -> float:
    clean = list(values)
    return sum(clean) / len(clean) if clean else 0.0


def _std(values: Iterable[float]) -> float:
    clean = list(values)
    if len(clean) < 2:
        return 0.0
    mean = _mean(clean)
    return math.sqrt(sum((value - mean) ** 2 for value in clean) / (len(clean) - 1))


def _configured_horizons(config: dict[str, Any], spec: FamilySpec) -> list[int]:
    return [int(value) for value in cfg_get(config, f"{spec.diagnostics_key}.horizons_trading_days", [21, 63])]


def _gate_value(config: dict[str, Any], spec: FamilySpec, prefix: str, horizon: int, default: float) -> float:
    return float(cfg_get(config, f"{spec.config_key}.{prefix}_{horizon}", default))


def _summary_metrics(row: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    marker = f"{prefix}_"
    return {str(key)[len(marker):]: value for key, value in row.items() if str(key).startswith(marker)}


def _date_values(rows: list[dict[str, Any]], horizon: int, field: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        if int(_number(row.get("horizon_days"), -1)) != horizon:
            continue
        raw = row.get(field)
        if raw not in (None, ""):
            values.append(_number(raw))
    return values


def _unique_date_values(rows: list[dict[str, Any]], field: str) -> list[float]:
    by_date: dict[str, float] = {}
    for row in rows:
        raw = row.get(field)
        asof = str(row.get("asof_date") or "")
        if asof and raw not in (None, ""):
            by_date[asof] = _number(raw)
    return list(by_date.values())


def _fold_objective(
    rows: list[dict[str, Any]],
    *,
    horizons: list[int],
    turnover_cost_bps: float,
    stability_lambda: float,
    complexity_penalty: float,
    nonzero_subfeatures: float,
    max_turnover: float,
    max_cohort_share: float,
) -> tuple[float, dict[str, float]]:
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else primary
    ic_primary = _date_values(rows, primary, "ic")
    ic_secondary = _date_values(rows, secondary, "ic")
    spread_primary = _date_values(rows, primary, "q5_minus_q1_fwd_resid")
    spread_secondary = _date_values(rows, secondary, "q5_minus_q1_fwd_resid")
    turnover = _mean(_unique_date_values(rows, "top_turnover"))
    cohort = _mean(_unique_date_values(rows, "top_max_cohort_share"))
    cost_drag = turnover * 2.0 * turnover_cost_bps / 10000.0
    metrics = {
        f"mean_ic_{primary}": _mean(ic_primary),
        f"mean_ic_{secondary}": _mean(ic_secondary),
        f"hit_rate_{primary}": _mean(1.0 if value > 0 else 0.0 for value in ic_primary),
        f"hit_rate_{secondary}": _mean(1.0 if value > 0 else 0.0 for value in ic_secondary),
        f"mean_spread_net_{primary}": _mean(spread_primary) - cost_drag,
        f"mean_spread_net_{secondary}": _mean(spread_secondary) - cost_drag,
        "avg_top_turnover": turnover,
        "avg_top_cohort_share": cohort,
    }
    objective = (
        0.50 * metrics[f"mean_ic_{primary}"]
        + 0.36 * metrics[f"mean_ic_{secondary}"]
        + 0.05 * (metrics[f"hit_rate_{primary}"] - 0.50)
        + 0.04 * (metrics[f"hit_rate_{secondary}"] - 0.50)
        + 0.03 * metrics[f"mean_spread_net_{primary}"]
        + 0.02 * metrics[f"mean_spread_net_{secondary}"]
        - stability_lambda * _std(ic_primary)
        - complexity_penalty * nonzero_subfeatures
        - max(0.0, turnover - max_turnover) * 0.08
        - max(0.0, cohort - max_cohort_share) * 0.10
    )
    return objective, metrics


def _holdout_folds(
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    spec: FamilySpec,
    horizons: list[int],
    candidate_nonzero: float,
    baseline_nonzero: float,
) -> tuple[list[dict[str, Any]], float]:
    dates = sorted({str(row.get("asof_date") or "") for row in candidate_rows if row.get("asof_date")})
    fold_count = max(1, int(cfg_get(config, f"{spec.config_key}.robustness_folds", 5)))
    turnover_cost = float(cfg_get(config, f"{spec.config_key}.turnover_cost_bps", 20.0))
    stability = float(cfg_get(config, f"{spec.config_key}.stability_lambda", 0.10))
    complexity = float(cfg_get(config, f"{spec.config_key}.complexity_penalty_per_subfeature", 0.0005))
    max_turnover = float(cfg_get(config, f"{spec.config_key}.max_turnover", 0.60))
    max_cohort = float(cfg_get(config, f"{spec.config_key}.max_top_cohort_share", 0.55))
    date_folds = [dates[int(index * len(dates) / fold_count): int((index + 1) * len(dates) / fold_count)] for index in range(fold_count)]
    rows: list[dict[str, Any]] = []
    wins = 0
    for index, fold_dates in enumerate(date_folds):
        if not fold_dates:
            continue
        date_set = set(fold_dates)
        candidate_fold = [row for row in candidate_rows if str(row.get("asof_date") or "") in date_set]
        baseline_fold = [row for row in baseline_rows if str(row.get("asof_date") or "") in date_set]
        candidate_objective, candidate_metrics = _fold_objective(
            candidate_fold,
            horizons=horizons,
            turnover_cost_bps=turnover_cost,
            stability_lambda=stability,
            complexity_penalty=complexity,
            nonzero_subfeatures=candidate_nonzero,
            max_turnover=max_turnover,
            max_cohort_share=max_cohort,
        )
        baseline_objective, baseline_metrics = _fold_objective(
            baseline_fold,
            horizons=horizons,
            turnover_cost_bps=turnover_cost,
            stability_lambda=stability,
            complexity_penalty=complexity,
            nonzero_subfeatures=baseline_nonzero,
            max_turnover=max_turnover,
            max_cohort_share=max_cohort,
        )
        win = int(candidate_objective > baseline_objective)
        wins += win
        rows.append(
            {
                "fold": index,
                "fold_source": "untouched_holdout_only",
                "fold_start": fold_dates[0],
                "fold_end": fold_dates[-1],
                "n_dates": len(fold_dates),
                "stage7_objective": baseline_objective,
                "stage8_objective": candidate_objective,
                "stage8_wins": win,
                **{f"stage7_{key}": value for key, value in baseline_metrics.items()},
                **{f"stage8_{key}": value for key, value in candidate_metrics.items()},
            }
        )
    return rows, wins / len(rows) if rows else 0.0


def _stage8_evidence(
    *,
    config: dict[str, Any],
    spec: FamilySpec,
    summary_rows: list[dict[str, Any]],
    candidate_date_rows: list[dict[str, Any]],
    baseline_date_rows: list[dict[str, Any]],
    post_lock_data_included: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if len(summary_rows) < 2:
        raise RuntimeError("Stage 8 summary must contain baseline and candidate rows.")
    candidate = next((row for row in summary_rows if row.get("model") == "stage8_best_candidate"), None)
    baseline = next((row for row in summary_rows if row.get("model") == "stage7_baseline"), None)
    if candidate is None or baseline is None:
        raise RuntimeError("Stage 8 summary model labels are incomplete.")
    horizons = _configured_horizons(config, spec)
    step = int(cfg_get(config, f"{spec.diagnostics_key}.step_trading_days", 21))
    for row, date_rows in ((candidate, candidate_date_rows), (baseline, baseline_date_rows)):
        for horizon in horizons:
            ic_values = _date_values(date_rows, horizon, "ic")
            row[f"holdout_newey_west_t_stat_{horizon}"] = newey_west_t_stat(
                ic_values,
                lags=newey_west_lags_for_horizon(horizon, step),
            )
    fold_rows, fold_win_fraction = _holdout_folds(
        candidate_date_rows,
        baseline_date_rows,
        config=config,
        spec=spec,
        horizons=horizons,
        candidate_nonzero=_number(candidate.get("holdout_nonzero_subfeatures")),
        baseline_nonzero=_number(baseline.get("holdout_nonzero_subfeatures")),
    )
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else primary
    candidate_metrics = _summary_metrics(candidate, "holdout")
    baseline_metrics = _summary_metrics(baseline, "holdout")
    decision = stage8_gate_decision(
        candidate=candidate_metrics,
        baseline=baseline_metrics,
        primary_horizon=primary,
        secondary_horizon=secondary,
        min_objective_improvement=float(cfg_get(config, f"{spec.config_key}.promotion_min_objective_improvement", 0.002)),
        min_ic_primary=_gate_value(config, spec, "min_holdout_mean_ic", primary, 0.005),
        min_ic_secondary=_gate_value(config, spec, "min_holdout_mean_ic", secondary, 0.005),
        min_newey_west_t_primary=_gate_value(config, spec, "min_holdout_newey_west_t_stat", primary, 2.0),
        min_newey_west_t_secondary=_gate_value(config, spec, "min_holdout_newey_west_t_stat", secondary, 2.0),
        min_hit_rate=float(cfg_get(config, f"{spec.config_key}.min_holdout_hit_rate", 0.50)),
        min_spread_primary=_gate_value(config, spec, "min_holdout_mean_spread_net", primary, 0.0),
        min_spread_secondary=_gate_value(config, spec, "min_holdout_mean_spread_net", secondary, 0.0),
        max_turnover=float(cfg_get(config, f"{spec.config_key}.max_turnover", 0.60)),
        max_cohort_share=float(cfg_get(config, f"{spec.config_key}.max_top_cohort_share", 0.55)),
        fold_win_fraction=fold_win_fraction,
        min_fold_win_fraction=float(cfg_get(config, f"{spec.config_key}.min_fold_win_fraction", 0.50)),
        post_lock_data_included=post_lock_data_included,
    )
    for row in summary_rows:
        row["fold_scope"] = "untouched_holdout_only"
        row["fold_win_fraction"] = fold_win_fraction if row.get("model") == "stage8_best_candidate" else ""
        row["stage8_gate_pass"] = int(decision.passed) if row.get("model") == "stage8_best_candidate" else 0
        row["stage8_gate_reasons"] = ";".join(decision.reasons) if row.get("model") == "stage8_best_candidate" else "baseline"
        # Stage 8 is preliminary by construction. Only Stage 8C can declare a
        # candidate finally eligible for manual promotion.
        row["promotion_candidate"] = 0
        row["promotion_scope"] = "preliminary_requires_walk_forward"
    return summary_rows, fold_rows, {
        "stage8_gate_pass": int(decision.passed),
        "stage8_gate_reasons": list(decision.reasons),
        "fold_win_fraction": fold_win_fraction,
        "fold_scope": "untouched_holdout_only",
        "robustness_folds": len(fold_rows),
    }


STAGE8_ARTIFACTS = (
    "stage8_trials.csv",
    "stage8_best_summary.csv",
    "stage8_best_train_by_date.csv",
    "stage8_best_holdout_by_date.csv",
    "stage8_stage7_holdout_by_date.csv",
    "stage8_stage7_full_by_date.csv",
    "stage8_fold_robustness.csv",
    "stage8_candidate_current_scores.csv",
    "stage8_best_weights.json",
)


def _require_native_run_attestation(paths: Iterable[Path], *, started_ns: int | None) -> None:
    if started_ns is None:
        raise RuntimeError("Refusing to seal an unattested calibration run; use the official family runner.")
    stale = [str(path) for path in paths if not path.exists() or path.stat().st_mtime_ns + 2_000_000_000 < started_ns]
    if stale:
        raise RuntimeError("Native calibration did not freshly rewrite required artifacts: " + ", ".join(stale))


def harden_stage8(
    family: str,
    *,
    config_path: Path,
    output_dir: Path | None = None,
    native_run_started_ns: int | None = None,
) -> dict[str, Any]:
    spec, config, output, panel_path = _paths(family, config_path, output_dir)
    best_path = output / "stage8_best_weights.json"
    best = json.loads(best_path.read_text(encoding="utf-8"))
    required_native_paths = [output / name for name in STAGE8_ARTIFACTS]
    _require_native_run_attestation(required_native_paths, started_ns=native_run_started_ns)
    current_config_hash = sha256_file(config_path)
    if best.get("config_sha256") != current_config_hash:
        raise RuntimeError("Native Stage 8 output config hash is stale; rerun the optimizer under the current config.")
    if family == "semiconductors":
        source_panel = output / "stage8_stage7_full_by_date.csv"
        panel_temp = panel_path.with_suffix(f"{panel_path.suffix}.tmp")
        shutil.copyfile(source_panel, panel_temp)
        panel_temp.replace(panel_path)
    summary_rows = _read_csv(output / "stage8_best_summary.csv")
    candidate_dates = _read_csv(output / "stage8_best_holdout_by_date.csv")
    baseline_dates = _read_csv(output / "stage8_stage7_holdout_by_date.csv")
    summary_rows, fold_rows, evidence = _stage8_evidence(
        config=config,
        spec=spec,
        summary_rows=summary_rows,
        candidate_date_rows=candidate_dates,
        baseline_date_rows=baseline_dates,
        post_lock_data_included=bool(best.get("post_lock_data_included")),
    )
    config_hash = sha256_file(config_path)
    panel_hash_before_stamp = sha256_file(panel_path)
    run_id = new_run_id(family, "stage8", config_sha256=config_hash, panel_sha256=panel_hash_before_stamp)
    fields = {
        "calibration_run_id": run_id,
        "config_sha256": config_hash,
        "signal_panel_sha256": panel_hash_before_stamp,
    }
    _write_csv(output / "stage8_best_summary.csv", stamp_rows(summary_rows, **fields))
    _write_csv(output / "stage8_fold_robustness.csv", stamp_rows(fold_rows, **fields))
    for name in STAGE8_ARTIFACTS:
        path = output / name
        if path.suffix.lower() != ".csv" or name in {"stage8_best_summary.csv", "stage8_fold_robustness.csv"}:
            continue
        rows = _read_csv(path)
        if rows:
            _write_csv(path, stamp_rows(rows, **fields))
    best.update(
        {
            **evidence,
            "promotion_candidate": 0,
            "promotion_scope": "preliminary_requires_walk_forward",
            "calibration_run_id": run_id,
            "config_sha256": config_hash,
            "signal_panel_sha256": sha256_file(panel_path),
            "governance_hardening_version": "technology_optuna_governance_v1",
        }
    )
    _write_json(best_path, best)
    artifacts = [name for name in STAGE8_ARTIFACTS if (output / name).exists() and (output / name).stat().st_size > 0]
    if family == "semiconductors":
        artifacts.append(panel_path.name)
    return seal_calibration_run(
        output_dir=output,
        manifest_filename=spec.stage8_manifest,
        run_id=run_id,
        model_family=family,
        stage="stage8",
        config_path=config_path,
        panel_path=panel_path,
        artifact_names=artifacts,
        metadata={"stage8_gate_pass": evidence["stage8_gate_pass"], "promotion_candidate": 0},
    )


def _compare_float(errors: list[str], label: str, actual: Any, expected: Any, tolerance: float = 1e-10) -> None:
    if abs(_number(actual) - _number(expected)) > tolerance:
        errors.append(f"{label} mismatch: actual={actual} expected={expected}")


def validate_stage8(family: str, *, config_path: Path, output_dir: Path | None = None) -> list[str]:
    spec, config, output, panel_path = _paths(family, config_path, output_dir)
    errors = validate_calibration_run_manifest(
        output / spec.stage8_manifest,
        expected_model_family=family,
        expected_stage="stage8",
        current_config_path=config_path,
        current_panel_path=panel_path,
    )
    try:
        best = json.loads((output / "stage8_best_weights.json").read_text(encoding="utf-8"))
        summaries = _read_csv(output / "stage8_best_summary.csv")
        expected_summaries, expected_folds, evidence = _stage8_evidence(
            config=config,
            spec=spec,
            summary_rows=[dict(row) for row in summaries],
            candidate_date_rows=_read_csv(output / "stage8_best_holdout_by_date.csv"),
            baseline_date_rows=_read_csv(output / "stage8_stage7_holdout_by_date.csv"),
            post_lock_data_included=bool(best.get("post_lock_data_included")),
        )
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        return [*errors, f"Unable to recompute Stage 8 governance evidence: {exc}"]
    if int(best.get("promotion_candidate") or 0) != 0:
        errors.append("Stage 8 artifact incorrectly claims final promotion eligibility.")
    if int(best.get("stage8_gate_pass") or 0) != int(evidence["stage8_gate_pass"]):
        errors.append("Stage 8 gate flag does not match independently recomputed evidence.")
    if list(best.get("stage8_gate_reasons") or []) != list(evidence["stage8_gate_reasons"]):
        errors.append("Stage 8 gate reasons do not match independently recomputed evidence.")
    _compare_float(errors, "fold_win_fraction", best.get("fold_win_fraction"), evidence["fold_win_fraction"])
    stored_folds = _read_csv(output / "stage8_fold_robustness.csv")
    if len(stored_folds) != len(expected_folds):
        errors.append(f"Stage 8 holdout-fold count mismatch: {len(stored_folds)} != {len(expected_folds)}")
    if any(row.get("fold_source") != "untouched_holdout_only" for row in stored_folds):
        errors.append("Stage 8 robustness contains a fold outside the untouched holdout scope.")
    candidate = next((row for row in summaries if row.get("model") == "stage8_best_candidate"), {})
    expected_candidate = next((row for row in expected_summaries if row.get("model") == "stage8_best_candidate"), {})
    for horizon in _configured_horizons(config, spec):
        _compare_float(
            errors,
            f"holdout_newey_west_t_stat_{horizon}",
            candidate.get(f"holdout_newey_west_t_stat_{horizon}"),
            expected_candidate.get(f"holdout_newey_west_t_stat_{horizon}"),
        )
    manifest_path = output / spec.stage8_manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if best.get("calibration_run_id") != manifest.get("run_id"):
        errors.append("Stage 8 weights run_id does not match the sealed manifest.")
    return errors


def _walk_forward_evidence(
    *,
    family: str,
    config: dict[str, Any],
    spec: FamilySpec,
    block_rows: list[dict[str, Any]],
    stage8: Mapping[str, Any],
    post_lock_data_included: bool,
) -> dict[str, Any]:
    if not block_rows:
        raise RuntimeError("Walk-forward block file is empty.")
    horizons = _configured_horizons(config, spec)
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else primary
    max_turnover = float(cfg_get(config, f"{spec.config_key}.max_turnover", 0.60))
    max_cohort = float(cfg_get(config, f"{spec.config_key}.max_top_cohort_share", 0.55))
    cohort_cap_tolerance = float(
        cfg_get(config, f"{spec.config_key}.walk_forward.max_top_cohort_share_incumbent_tolerance", 0.0)
    )
    min_ic_primary = _gate_value(config, spec, "min_holdout_mean_ic", primary, 0.005)
    min_ic_secondary = _gate_value(config, spec, "min_holdout_mean_ic", secondary, 0.005)
    min_hit = float(cfg_get(config, f"{spec.config_key}.min_holdout_hit_rate", 0.50))
    min_spread_primary = _gate_value(config, spec, "min_holdout_mean_spread_net", primary, 0.0)
    min_spread_secondary = _gate_value(config, spec, "min_holdout_mean_spread_net", secondary, 0.0)
    improvements = [_number(row.get("objective_improvement")) for row in block_rows]
    constraint_flags: list[int] = []
    gate_flags: list[int] = []
    for row in block_rows:
        incumbent_cohort_share = _number(row.get("stage7_avg_top_cohort_share"), max_cohort)
        effective_cohort_cap = incumbent_relative_cohort_cap(
            max_cohort,
            incumbent_cohort_share,
            cohort_cap_tolerance,
        )
        row["configured_max_top_cohort_share"] = max_cohort
        row["effective_max_top_cohort_share"] = effective_cohort_cap
        constraint = int(
            _number(row.get("refit_avg_top_turnover"), 1.0) <= max_turnover
            and _number(row.get("refit_avg_top_cohort_share"), 1.0) <= effective_cohort_cap
        )
        gate = int(
            _number(row.get("objective_improvement")) > 0
            and constraint == 1
            and _number(row.get(f"refit_mean_ic_{primary}")) >= min_ic_primary
            and _number(row.get(f"refit_mean_ic_{secondary}")) >= min_ic_secondary
            and _number(row.get(f"refit_hit_rate_{primary}"), -1.0) >= min_hit
            and _number(row.get(f"refit_mean_spread_net_{primary}"), -1.0) >= min_spread_primary
            and _number(row.get(f"refit_mean_spread_net_{secondary}"), -1.0) >= min_spread_secondary
        )
        row["constraint_pass"] = constraint
        row["promotion_gate_pass"] = gate
        constraint_flags.append(constraint)
        gate_flags.append(gate)
    summary: dict[str, Any] = {
        "source_id": f"{family}_stage8_walk_forward_calibration",
        "n_blocks": len(block_rows),
        "refit_win_rate": _mean(1.0 if value > 0 else 0.0 for value in improvements),
        "promotion_gate_pass_rate": _mean(float(value) for value in gate_flags),
        "constraint_pass_rate": _mean(float(value) for value in constraint_flags),
        "configured_max_top_cohort_share": max_cohort,
        "max_top_cohort_share_incumbent_tolerance": cohort_cap_tolerance,
        "adaptive_cohort_cap_policy": (
            "max_configured_or_incumbent_plus_tolerance"
            if cohort_cap_tolerance > 0.0
            else "configured_absolute_cap"
        ),
        "mean_objective_improvement": _mean(improvements),
        "improvement_paired_t": raw_t_stat(improvements),
        f"mean_refit_oos_ic_{primary}": _mean(_number(row.get(f"refit_mean_ic_{primary}")) for row in block_rows),
        f"mean_refit_oos_ic_{secondary}": _mean(_number(row.get(f"refit_mean_ic_{secondary}")) for row in block_rows),
        f"mean_refit_hit_rate_{primary}": _mean(_number(row.get(f"refit_hit_rate_{primary}"), -1.0) for row in block_rows),
        f"mean_refit_spread_net_{primary}": _mean(_number(row.get(f"refit_mean_spread_net_{primary}"), -1.0) for row in block_rows),
        f"mean_refit_spread_net_{secondary}": _mean(_number(row.get(f"refit_mean_spread_net_{secondary}"), -1.0) for row in block_rows),
        "post_lock_data_included": post_lock_data_included,
    }
    min_win_rate = float(cfg_get(config, f"{spec.config_key}.min_fold_win_fraction", 0.50))
    min_gate_rate = float(cfg_get(config, f"{spec.config_key}.walk_forward.min_gate_pass_rate", 0.50))
    min_paired_t = float(cfg_get(config, f"{spec.config_key}.walk_forward.min_paired_t", 2.0))
    decision = walk_forward_gate_decision(
        summary,
        min_win_rate=min_win_rate,
        min_gate_pass_rate=min_gate_rate,
        min_constraint_pass_rate=min_win_rate,
        min_paired_t=min_paired_t,
    )
    domain_reasons: list[str] = []
    if summary[f"mean_refit_oos_ic_{primary}"] < min_ic_primary:
        domain_reasons.append(f"mean_refit_oos_ic_{primary}_below_minimum")
    if summary[f"mean_refit_oos_ic_{secondary}"] < min_ic_secondary:
        domain_reasons.append(f"mean_refit_oos_ic_{secondary}_below_minimum")
    if summary[f"mean_refit_hit_rate_{primary}"] < min_hit:
        domain_reasons.append(f"mean_refit_hit_rate_{primary}_below_minimum")
    if summary[f"mean_refit_spread_net_{primary}"] < min_spread_primary:
        domain_reasons.append(f"mean_refit_spread_net_{primary}_below_minimum")
    if summary[f"mean_refit_spread_net_{secondary}"] < min_spread_secondary:
        domain_reasons.append(f"mean_refit_spread_net_{secondary}_below_minimum")
    procedure_reasons = list(dict.fromkeys([*decision.reasons, *domain_reasons]))
    summary["procedure_adds_value"] = int(not procedure_reasons)
    summary["procedure_gate_reasons"] = procedure_reasons
    summary["config_sha256"] = stage8.get("config_sha256", "")
    summary["signal_panel_sha256"] = stage8.get("signal_panel_sha256", "")
    final = final_promotion_decision(
        stage8,
        summary,
        min_paired_t=min_paired_t,
        min_gate_pass_rate=min_gate_rate,
        min_win_rate=min_win_rate,
        min_constraint_pass_rate=min_win_rate,
    )
    final_reasons = list(final.reasons)
    if procedure_reasons:
        final_reasons.extend(procedure_reasons)
    summary["final_promotion_eligible"] = int(final.passed and not procedure_reasons)
    summary["final_promotion_reasons"] = list(dict.fromkeys(final_reasons))
    summary["stage8_run_id"] = stage8.get("calibration_run_id", "")
    return summary


WALK_FORWARD_ARTIFACTS = ("walk_forward_blocks.csv", "walk_forward_summary.csv", "walk_forward_summary.json")


def harden_walk_forward(
    family: str,
    *,
    config_path: Path,
    output_dir: Path | None = None,
    native_run_started_ns: int | None = None,
) -> dict[str, Any]:
    spec, config, base_output, panel_path = _paths(family, config_path, output_dir)
    output = base_output / str(cfg_get(config, f"{spec.config_key}.walk_forward.output_subdir", "walk_forward"))
    stage8 = json.loads((base_output / "stage8_best_weights.json").read_text(encoding="utf-8"))
    blocks = _read_csv(output / "walk_forward_blocks.csv")
    native_summary_path = output / "walk_forward_summary.json"
    native_summary = json.loads(native_summary_path.read_text(encoding="utf-8"))
    _require_native_run_attestation(
        [output / name for name in WALK_FORWARD_ARTIFACTS],
        started_ns=native_run_started_ns,
    )
    if native_summary.get("config_sha256") != sha256_file(config_path):
        raise RuntimeError("Native walk-forward output config hash is stale; rerun under the current config.")
    summary = _walk_forward_evidence(
        family=family,
        config=config,
        spec=spec,
        block_rows=blocks,
        stage8=stage8,
        post_lock_data_included=bool(native_summary.get("post_lock_data_included")),
    )
    for key in (
        "n_trials_per_refit",
        "initial_train_dates",
        "test_block_dates",
        "embargo_panel_dates",
        "random_seed",
        "calibration_train_end_date",
        "panel_end_cap_date",
    ):
        if key in native_summary:
            summary[key] = native_summary[key]
    config_hash = sha256_file(config_path)
    panel_hash = sha256_file(panel_path)
    run_id = new_run_id(family, "walk_forward", config_sha256=config_hash, panel_sha256=panel_hash)
    fields = {
        "calibration_run_id": run_id,
        "config_sha256": config_hash,
        "signal_panel_sha256": panel_hash,
        "stage8_run_id": stage8.get("calibration_run_id", ""),
    }
    summary.update(fields)
    _write_csv(output / "walk_forward_blocks.csv", stamp_rows(blocks, **fields))
    _write_csv(output / "walk_forward_summary.csv", [summary])
    _write_json(output / "walk_forward_summary.json", summary)
    return seal_calibration_run(
        output_dir=output,
        manifest_filename="walk_forward_run_manifest.json",
        run_id=run_id,
        model_family=family,
        stage="walk_forward",
        config_path=config_path,
        panel_path=panel_path,
        artifact_names=WALK_FORWARD_ARTIFACTS,
        metadata={
            "procedure_adds_value": summary["procedure_adds_value"],
            "final_promotion_eligible": summary["final_promotion_eligible"],
        },
    )


def validate_walk_forward(family: str, *, config_path: Path, output_dir: Path | None = None) -> list[str]:
    spec, config, base_output, panel_path = _paths(family, config_path, output_dir)
    output = base_output / str(cfg_get(config, f"{spec.config_key}.walk_forward.output_subdir", "walk_forward"))
    errors = validate_calibration_run_manifest(
        output / "walk_forward_run_manifest.json",
        expected_model_family=family,
        expected_stage="walk_forward",
        current_config_path=config_path,
        current_panel_path=panel_path,
    )
    try:
        stage8 = json.loads((base_output / "stage8_best_weights.json").read_text(encoding="utf-8"))
        summary = json.loads((output / "walk_forward_summary.json").read_text(encoding="utf-8"))
        blocks = _read_csv(output / "walk_forward_blocks.csv")
        expected = _walk_forward_evidence(
            family=family,
            config=config,
            spec=spec,
            block_rows=[dict(row) for row in blocks],
            stage8=stage8,
            post_lock_data_included=bool(summary.get("post_lock_data_included")),
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return [*errors, f"Unable to recompute walk-forward governance evidence: {exc}"]
    for key in (
        "refit_win_rate",
        "promotion_gate_pass_rate",
        "constraint_pass_rate",
        "mean_objective_improvement",
        "improvement_paired_t",
    ):
        _compare_float(errors, key, summary.get(key), expected.get(key))
    for key in ("procedure_adds_value", "final_promotion_eligible"):
        if int(summary.get(key) or 0) != int(expected.get(key) or 0):
            errors.append(f"Walk-forward {key} does not match independently recomputed evidence.")
    if list(summary.get("procedure_gate_reasons") or []) != list(expected.get("procedure_gate_reasons") or []):
        errors.append("Walk-forward procedure gate reasons mismatch.")
    if list(summary.get("final_promotion_reasons") or []) != list(expected.get("final_promotion_reasons") or []):
        errors.append("Final promotion reasons mismatch.")
    if summary.get("stage8_run_id") != stage8.get("calibration_run_id"):
        errors.append("Walk-forward evidence is not bound to the current Stage 8 run.")
    manifest_path = output / "walk_forward_run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if summary.get("calibration_run_id") != manifest.get("run_id"):
        errors.append("Walk-forward summary run_id does not match the sealed manifest.")
    return errors


def run_stage8_with_governance(native_runner: Any, family: str) -> None:
    started_ns = time.time_ns()
    native_runner()
    args = _args_from_argv()
    manifest = harden_stage8(
        family,
        config_path=args.config.expanduser().resolve(),
        output_dir=args.output_dir,
        native_run_started_ns=started_ns,
    )
    LOGGER.info("Sealed Stage 8 run %s", manifest["run_id"])


def run_walk_forward_with_governance(native_runner: Any, family: str) -> None:
    args = _args_from_argv()
    config_path = args.config.expanduser().resolve()
    stage8_errors = validate_stage8(family, config_path=config_path, output_dir=args.output_dir)
    if stage8_errors:
        raise RuntimeError(
            "Refusing walk-forward execution because Stage 8 is not a current, sealed parent run: "
            + "; ".join(stage8_errors)
        )
    started_ns = time.time_ns()
    native_runner()
    manifest = harden_walk_forward(
        family,
        config_path=config_path,
        output_dir=args.output_dir,
        native_run_started_ns=started_ns,
    )
    LOGGER.info("Sealed walk-forward run %s", manifest["run_id"])


def validate_stage8_from_argv(family: str) -> int:
    args = _args_from_argv()
    errors = validate_stage8(family, config_path=args.config.expanduser().resolve(), output_dir=args.output_dir)
    for error in errors:
        LOGGER.error(error)
    return int(bool(errors))


def validate_walk_forward_from_argv(family: str) -> int:
    args = _args_from_argv()
    errors = validate_walk_forward(family, config_path=args.config.expanduser().resolve(), output_dir=args.output_dir)
    for error in errors:
        LOGGER.error(error)
    return int(bool(errors))
