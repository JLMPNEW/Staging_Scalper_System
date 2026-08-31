from __future__ import annotations

# ruff: noqa: E402

import argparse
import csv
import hashlib
import json
import logging
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.calibrated_scoring import (
    component_weight_specs as calibrated_component_weight_specs,
)
from technology.core.calibrated_scoring import (
    subfeature_weight_specs as calibrated_subfeature_weight_specs,
)
from technology.core.calibration_governance import incumbent_relative_cohort_cap
from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.scoring_features import SUBFEATURE_SPECS, percentile_scores, safe_float, weighted_available_score
from technology.core.signal_diagnostics import quintile_spread, raw_t_stat, spearman
from technology.core.text_norm import normalize_ticker
from technology.software_infrastructure.calibrated_scoring import SETTINGS as STAGE7_SETTINGS


LOGGER = logging.getLogger("software_infrastructure_optuna")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_optuna_calibration"


@dataclass(frozen=True)
class Candidate:
    component_weights: dict[str, float]
    subfeature_specs: dict[str, list[tuple[str, float]]]


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--n-trials", type=int, default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument(
        "--allow-post-lock-panel",
        action="store_true",
        help=(
            "Calibrate on panel dates past the configured calibration_train_end_date. "
            "Stamps post_lock_data_included=true in the output JSON."
        ),
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    clean = {key: max(0.0, float(value)) for key, value in raw.items()}
    total = sum(clean.values())
    if total <= 0:
        return {key: 0.0 for key in clean}
    return {key: value / total for key, value in clean.items()}


def configured_components(config: dict[str, Any]) -> list[str]:
    raw = cfg_get(config, f"{CONFIG_KEY}.component_bounds", {}) or {}
    if isinstance(raw, dict) and raw:
        return [str(key) for key in raw]
    return ["quality", "valuation", "growth", "market_behavior", "positioning", "risk_control"]


def component_bounds(config: dict[str, Any]) -> dict[str, tuple[float, float]]:
    defaults = {
        "quality": (0.15, 0.40),
        "valuation": (0.05, 0.30),
        "growth": (0.00, 0.08),
        "market_behavior": (0.10, 0.30),
        "positioning": (0.00, 0.12),
        "risk_control": (0.15, 0.35),
    }
    raw = cfg_get(config, f"{CONFIG_KEY}.component_bounds", {}) or {}
    out: dict[str, tuple[float, float]] = {}
    for component in configured_components(config):
        default = defaults.get(component, (0.0, 1.0))
        value = raw.get(component) if isinstance(raw, dict) else None
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            lo = safe_float(value[0])
            hi = safe_float(value[1])
            out[component] = (float(lo if lo is not None else default[0]), float(hi if hi is not None else default[1]))
        else:
            out[component] = default
    return out


def enforce_component_bounds(weights: dict[str, float], bounds: dict[str, tuple[float, float]]) -> dict[str, float]:
    adjusted = dict(weights)
    for _ in range(32):
        fixed_total = 0.0
        free_keys: list[str] = []
        changed = False
        for key, value in list(adjusted.items()):
            lo, hi = bounds.get(key, (0.0, 1.0))
            if value < lo:
                adjusted[key] = lo
                fixed_total += lo
                changed = True
            elif value > hi:
                adjusted[key] = hi
                fixed_total += hi
                changed = True
            else:
                free_keys.append(key)
        remaining = max(0.0, 1.0 - fixed_total)
        free_total = sum(adjusted[key] for key in free_keys)
        if free_keys and free_total > 0:
            for key in free_keys:
                adjusted[key] = adjusted[key] / free_total * remaining
        elif free_keys:
            equal = remaining / len(free_keys)
            for key in free_keys:
                adjusted[key] = equal
        if not changed:
            break
    return adjusted


def stage7_candidate(config: dict[str, Any]) -> Candidate:
    return Candidate(
        component_weights=calibrated_component_weight_specs(config, STAGE7_SETTINGS),
        subfeature_specs=calibrated_subfeature_weight_specs(config, STAGE7_SETTINGS),
    )


def candidate_subfeature_keys(config: dict[str, Any]) -> dict[str, list[str]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.subfeature_candidates", {}) or {}
    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for component, values in raw.items():
            if isinstance(values, (list, tuple)):
                out[str(component)] = [str(value) for value in values]
    if out:
        return out
    return {component: [score for score, weight in specs if weight > 0] for component, specs in stage7_candidate(config).subfeature_specs.items()}


def subfeature_effective_bounds(config: dict[str, Any]) -> dict[tuple[str, str], tuple[float, float]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.subfeature_effective_weight_bounds", {}) or {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    if not isinstance(raw, dict):
        return out
    for component, weights in raw.items():
        if not isinstance(weights, dict):
            continue
        for score_key, bounds in weights.items():
            if not isinstance(bounds, (list, tuple)) or len(bounds) < 2:
                continue
            lo = safe_float(bounds[0])
            hi = safe_float(bounds[1])
            out[(str(component), str(score_key))] = (
                max(0.0, float(lo if lo is not None else 0.0)),
                max(0.0, float(hi if hi is not None else 1.0)),
            )
    return out


def enforce_subfeature_effective_bounds(
    component: str,
    component_weight: float,
    weights: dict[str, float],
    bounds: dict[tuple[str, str], tuple[float, float]],
) -> dict[str, float]:
    if component_weight <= 0 or not weights:
        return weights
    adjusted = dict(weights)
    for _ in range(32):
        changed = False
        fixed: dict[str, float] = {}
        free_keys: list[str] = []
        for score_key, weight in adjusted.items():
            lo_eff, hi_eff = bounds.get((component, score_key), (0.0, component_weight))
            lo = min(1.0, max(0.0, lo_eff / component_weight))
            hi = min(1.0, max(lo, hi_eff / component_weight))
            if weight < lo:
                fixed[score_key] = lo
                changed = True
            elif weight > hi:
                fixed[score_key] = hi
                changed = True
            else:
                free_keys.append(score_key)
        remaining = max(0.0, 1.0 - sum(fixed.values()))
        free_total = sum(adjusted[key] for key in free_keys)
        if free_keys and free_total > 0:
            for key in free_keys:
                adjusted[key] = adjusted[key] / free_total * remaining
        elif free_keys:
            equal = remaining / len(free_keys)
            for key in free_keys:
                adjusted[key] = equal
        for key, value in fixed.items():
            adjusted[key] = value
        if not changed:
            break
    total = sum(max(0.0, value) for value in adjusted.values())
    return {key: max(0.0, value) / total for key, value in adjusted.items()} if total > 0 else adjusted


def sample_candidate(trial: Any, config: dict[str, Any], bounds: dict[str, tuple[float, float]]) -> Candidate:
    raw_component_weights = {
        component: trial.suggest_float(f"component__{component}", lo, hi) if hi > 0 else 0.0
        for component, (lo, hi) in bounds.items()
    }
    component_weights = enforce_component_bounds(normalize_weights(raw_component_weights), bounds)
    effective_bounds = subfeature_effective_bounds(config)
    subfeature_specs: dict[str, list[tuple[str, float]]] = {}
    for component, score_keys in candidate_subfeature_keys(config).items():
        if bounds.get(component, (0.0, 1.0))[1] <= 0 or not score_keys:
            subfeature_specs[component] = []
            continue
        raw = {
            score_key: trial.suggest_float(f"subfeature__{component}__{score_key}", 0.0, 1.0)
            for score_key in score_keys
        }
        weights = normalize_weights(raw)
        weights = enforce_subfeature_effective_bounds(component, component_weights.get(component, 0.0), weights, effective_bounds)
        subfeature_specs[component] = [(score_key, weight) for score_key, weight in weights.items() if weight > 0]
    return Candidate(component_weights=component_weights, subfeature_specs=subfeature_specs)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["empty"], extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": 0.0, "std": 0.0, "hit_rate": 0.0, "t_stat": None}
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1)) if len(values) > 2 else 0.0
    t_stat = raw_t_stat(values)
    return {
        "n": len(values),
        "mean": mean,
        "std": std,
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
        "t_stat": t_stat,
    }


def load_panel(config: dict[str, Any], base_dir: Path) -> tuple[list[dict[str, Any]], list[date], list[int]]:
    diagnostics_dir = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.signal_diagnostics_output_dir", cfg_get(config, "software_infrastructure_signal_diagnostics.output_dir")),
        base_dir=base_dir,
    )
    panel_path = diagnostics_dir / "signal_panel.csv"
    if not panel_path.exists():
        raise RuntimeError(f"Missing Stage 8A panel. Run software signal diagnostics first: {panel_path}")
    horizons = [int(value) for value in cfg_get(config, "software_infrastructure_signal_diagnostics.horizons_trading_days", [21, 63])]
    excluded = set(cfg_get(config, "software_infrastructure_signal_diagnostics.excluded_subfeatures", []) or [])
    subfeature_specs = [spec for spec in SUBFEATURE_SPECS if spec[0] not in excluded]
    numeric_fields = {raw for raw, _score, _hib, _valid in subfeature_specs}
    numeric_fields.add("beta_to_benchmark")
    for horizon in horizons:
        numeric_fields.add(f"fwd_return_{horizon}d")
        numeric_fields.add(f"benchmark_return_{horizon}d")
        numeric_fields.add(f"fwd_resid_{horizon}d")

    rows: list[dict[str, Any]] = []
    with panel_path.open("r", encoding="utf-8", newline="") as handle:
        for raw_row in csv.DictReader(handle):
            asof = parse_date(raw_row.get("asof_date"))
            ticker = normalize_ticker(raw_row.get("ticker"))
            if asof is None or not ticker:
                continue
            row: dict[str, Any] = {
                "asof_date": asof,
                "ticker": ticker,
                "cohort": raw_row.get("calibration_cohort_id") or "",
            }
            for field_name in numeric_fields:
                row[field_name] = safe_float(raw_row.get(field_name))
            rows.append(row)

    rows_by_date: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_date.setdefault(row["asof_date"], []).append(row)
    for date_rows in rows_by_date.values():
        for raw_key, score_key, higher_is_better, valid in subfeature_specs:
            scores = percentile_scores(date_rows, raw_key, higher_is_better=higher_is_better, valid=valid)
            for row in date_rows:
                row[score_key] = scores.get(str(row["ticker"]))
    panel_dates = sorted(rows_by_date)
    LOGGER.info("Loaded Stage 8A panel: rows=%d dates=%d path=%s", len(rows), len(panel_dates), panel_path)
    return rows, panel_dates, horizons


def calibration_train_end_date(config: dict[str, Any]) -> date:
    train_end = parse_date(
        cfg_get(config, "oos_calibration_standards.families.software_infrastructure.calibration_train_end_date", "")
    )
    if train_end is None:
        raise RuntimeError(
            "Missing oos_calibration_standards.families.software_infrastructure.calibration_train_end_date; "
            "cannot bound the Stage 8 calibration panel at the model lock."
        )
    return train_end


def cap_panel_to_train_end(
    panel: list[dict[str, Any]],
    panel_dates: list[date],
    horizons: list[int],
    config: dict[str, Any],
    *,
    allow_post_lock: bool,
) -> tuple[list[dict[str, Any]], list[date], dict[str, Any]]:
    """Drop panel dates at/after the calibration lock so no forward-return window crosses it.

    Panel rows carry forward returns of up to max(horizons) trading days, so in
    addition to dropping asof dates past calibration_train_end_date we trim the
    trailing ceil(max_horizon / step) panel dates whose forward windows would
    extend past the lock.
    """
    train_end = calibration_train_end_date(config)
    provenance: dict[str, Any] = {
        "calibration_train_end_date": train_end.isoformat(),
        "post_lock_data_included": False,
        "panel_dates_dropped_for_lock": 0,
    }
    if allow_post_lock:
        provenance["post_lock_data_included"] = True
        LOGGER.warning(
            "--allow-post-lock-panel set: calibrating on all %d panel dates without the %s train-end cap.",
            len(panel_dates),
            train_end.isoformat(),
        )
        return panel, panel_dates, provenance
    step = int(cfg_get(config, "software_infrastructure_signal_diagnostics.step_trading_days", 21))
    trailing = int(math.ceil(max(horizons) / max(1, step)))
    kept = [asof for asof in panel_dates if asof <= train_end]
    if trailing:
        kept = kept[:-trailing] if len(kept) > trailing else []
    dropped = len(panel_dates) - len(kept)
    provenance["panel_dates_dropped_for_lock"] = dropped
    if dropped:
        LOGGER.info(
            "Capped Stage 8 panel at calibration_train_end_date=%s: kept %d of %d panel dates "
            "(trailing %d dates trimmed so %d-day forward windows stay inside the lock).",
            train_end.isoformat(),
            len(kept),
            len(panel_dates),
            trailing,
            max(horizons),
        )
    kept_set = set(kept)
    return [row for row in panel if row["asof_date"] in kept_set], kept, provenance


def top_quantile_rows(scored_rows: list[dict[str, Any]], quantile: float, min_positions: int) -> list[dict[str, Any]]:
    ordered = sorted(scored_rows, key=lambda row: (-float(row["score"]), str(row["ticker"])))
    size = max(min_positions, int(math.ceil(len(ordered) * quantile)))
    return ordered[:size]


def score_row(row: dict[str, Any], candidate: Candidate, *, neutral_score: float) -> tuple[float, float, dict[str, float], dict[str, float]]:
    component_scores: dict[str, float] = {}
    component_quality: dict[str, float] = {}
    available_weight = 0.0
    weighted_score = 0.0
    weighted_quality = 0.0
    positive_weight = sum(weight for weight in candidate.component_weights.values() if weight > 0)
    for component, weight in candidate.component_weights.items():
        specs = candidate.subfeature_specs.get(component, [])
        if not specs:
            score = neutral_score
            quality = 0.0
        else:
            score, quality, _available, _missing, _detail = weighted_available_score(row, specs, neutral_score=neutral_score)
        component_scores[component] = score
        component_quality[component] = quality
        if weight > 0 and quality > 0:
            available_weight += weight
            weighted_score += score * weight
            weighted_quality += quality * weight
    if available_weight <= 0:
        return neutral_score, 0.0, component_scores, component_quality
    return weighted_score / available_weight, weighted_quality / positive_weight if positive_weight > 0 else 0.0, component_scores, component_quality


def evaluate_candidate(
    panel: list[dict[str, Any]],
    dates: list[date],
    horizons: list[int],
    candidate: Candidate,
    *,
    neutral_score: float,
    top_quantile: float,
    min_positions: int,
    max_turnover: float,
    max_top_cohort_share: float,
    min_cross_section: int,
    stability_lambda: float,
    complexity_penalty_per_subfeature: float,
    turnover_cost_bps: float,
    emit_date_rows: bool = False,
) -> dict[str, Any]:
    date_set = set(dates)
    rows_by_date: dict[date, list[dict[str, Any]]] = {}
    for row in panel:
        asof = row["asof_date"]
        if asof in date_set:
            rows_by_date.setdefault(asof, []).append(row)

    ic_values: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    spread_values: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    coverage_values: dict[int, list[int]] = {horizon: [] for horizon in horizons}
    date_rows: list[dict[str, Any]] = []
    turnovers: list[float] = []
    cohort_shares: list[float] = []
    score_ranges: list[float] = []
    prev_top: set[str] | None = None

    for asof in sorted(rows_by_date):
        scored_rows: list[dict[str, Any]] = []
        for row in rows_by_date[asof]:
            score, quality, _component_scores, _component_quality = score_row(row, candidate, neutral_score=neutral_score)
            if quality <= 0:
                continue
            scored = dict(row)
            scored["score"] = score
            scored["score_quality"] = quality
            scored_rows.append(scored)
        if len(scored_rows) < min_cross_section:
            continue

        scores = [float(row["score"]) for row in scored_rows]
        score_ranges.append(max(scores) - min(scores))
        top_rows = top_quantile_rows(scored_rows, top_quantile, min_positions)
        top = {str(row["ticker"]) for row in top_rows}
        if prev_top is not None and top:
            turnovers.append(1.0 - len(top & prev_top) / len(top))
        prev_top = top
        cohort_counts: dict[str, int] = {}
        for row in top_rows:
            cohort = str(row.get("cohort") or "")
            cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
        if top_rows:
            cohort_shares.append(max(cohort_counts.values()) / len(top_rows))

        for horizon in horizons:
            resid_key = f"fwd_resid_{horizon}d"
            pairs = [
                (float(row["score"]), float(row[resid_key]))
                for row in scored_rows
                if row.get(resid_key) is not None
            ]
            if len(pairs) < min_cross_section:
                continue
            ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            if ic is None:
                continue
            spread = quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            ic_values[horizon].append(ic)
            coverage_values[horizon].append(len(pairs))
            if spread is not None:
                spread_values[horizon].append(spread)
            if emit_date_rows:
                date_rows.append(
                    {
                        "asof_date": asof.isoformat(),
                        "horizon_days": horizon,
                        "ic": round(ic, 6),
                        "coverage": len(pairs),
                        "q5_minus_q1_fwd_resid": round(spread, 6) if spread is not None else "",
                        "top_turnover": round(turnovers[-1], 6) if turnovers else "",
                        "top_max_cohort_share": round(cohort_shares[-1], 6) if cohort_shares else "",
                    }
                )

    avg_turnover = sum(turnovers) / len(turnovers) if turnovers else 0.0
    cost_drag = avg_turnover * 2.0 * turnover_cost_bps / 10000.0
    nonzero_subfeatures = sum(
        len(candidate.subfeature_specs.get(component, []))
        for component, weight in candidate.component_weights.items()
        if weight > 0
    )
    metrics: dict[str, Any] = {
        "avg_top_turnover": avg_turnover,
        "avg_top_cohort_share": sum(cohort_shares) / len(cohort_shares) if cohort_shares else 0.0,
        "avg_score_range": sum(score_ranges) / len(score_ranges) if score_ranges else 0.0,
        "cost_drag_per_step": cost_drag,
        "nonzero_subfeatures": nonzero_subfeatures,
        "date_rows": date_rows,
    }
    for horizon in horizons:
        ic_stat = stats(ic_values[horizon])
        spread_stat = stats(spread_values[horizon])
        cov_stat = stats([float(value) for value in coverage_values[horizon]])
        metrics[f"mean_ic_{horizon}"] = ic_stat["mean"]
        metrics[f"std_ic_{horizon}"] = ic_stat["std"]
        metrics[f"t_stat_{horizon}"] = ic_stat["t_stat"]
        metrics[f"hit_rate_{horizon}"] = ic_stat["hit_rate"]
        metrics[f"n_dates_{horizon}"] = ic_stat["n"]
        metrics[f"mean_spread_{horizon}"] = spread_stat["mean"]
        metrics[f"mean_spread_net_{horizon}"] = spread_stat["mean"] - cost_drag
        metrics[f"avg_coverage_{horizon}"] = cov_stat["mean"]

    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    objective = (
        0.50 * float(metrics.get(f"mean_ic_{primary}", 0.0))
        + 0.36 * float(metrics.get(f"mean_ic_{secondary}", 0.0))
        + 0.05 * (float(metrics.get(f"hit_rate_{primary}", 0.0)) - 0.50)
        + 0.04 * (float(metrics.get(f"hit_rate_{secondary}", 0.0)) - 0.50)
        + 0.03 * float(metrics.get(f"mean_spread_net_{primary}", 0.0))
        + 0.02 * float(metrics.get(f"mean_spread_net_{secondary}", 0.0))
        - stability_lambda * float(metrics.get(f"std_ic_{primary}", 0.0))
        - complexity_penalty_per_subfeature * nonzero_subfeatures
    )
    turnover_penalty = max(0.0, float(metrics["avg_top_turnover"]) - max_turnover) * 0.08
    cohort_penalty = max(0.0, float(metrics["avg_top_cohort_share"]) - max_top_cohort_share) * 0.10
    metrics["constraint_penalty"] = turnover_penalty + cohort_penalty
    metrics["objective"] = objective - metrics["constraint_penalty"]
    return metrics


def split_dates(config: dict[str, Any], panel_dates: list[date], horizons: list[int]) -> tuple[list[date], list[date]]:
    holdout_fraction = float(cfg_get(config, f"{CONFIG_KEY}.holdout_fraction", 0.30))
    step = int(cfg_get(config, "software_infrastructure_signal_diagnostics.step_trading_days", 21))
    min_embargo = int(math.ceil(max(horizons) / max(1, step))) + 1
    embargo_dates = max(min_embargo, int(cfg_get(config, f"{CONFIG_KEY}.embargo_panel_dates", 4)))
    holdout_count = max(8, int(math.ceil(len(panel_dates) * holdout_fraction)))
    holdout_start = max(0, len(panel_dates) - holdout_count)
    train_end = max(0, holdout_start - embargo_dates)
    return panel_dates[:train_end], panel_dates[holdout_start:]


def contiguous_folds(panel_dates: list[date], folds: int) -> list[list[date]]:
    if folds <= 1 or len(panel_dates) < folds:
        return [list(panel_dates)]
    size = len(panel_dates) / folds
    return [panel_dates[int(i * size): int((i + 1) * size)] for i in range(folds)]


def json_ready_weights(candidate: Candidate) -> dict[str, Any]:
    return {
        "component_weights": candidate.component_weights,
        "subfeature_weights": {
            component: {score_key: weight for score_key, weight in specs}
            for component, specs in candidate.subfeature_specs.items()
        },
        "effective_subfeature_weights": {
            component: {
                score_key: candidate.component_weights.get(component, 0.0) * weight
                for score_key, weight in specs
            }
            for component, specs in candidate.subfeature_specs.items()
        },
    }


def flatten_metrics(prefix: str, metrics: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    out = {
        f"{prefix}_objective": metrics.get("objective"),
        f"{prefix}_avg_top_turnover": metrics.get("avg_top_turnover"),
        f"{prefix}_avg_top_cohort_share": metrics.get("avg_top_cohort_share"),
        f"{prefix}_avg_score_range": metrics.get("avg_score_range"),
        f"{prefix}_cost_drag_per_step": metrics.get("cost_drag_per_step"),
        f"{prefix}_nonzero_subfeatures": metrics.get("nonzero_subfeatures"),
        f"{prefix}_constraint_penalty": metrics.get("constraint_penalty"),
    }
    for horizon in horizons:
        for key in ("mean_ic", "std_ic", "t_stat", "hit_rate", "n_dates", "mean_spread", "mean_spread_net", "avg_coverage"):
            out[f"{prefix}_{key}_{horizon}"] = metrics.get(f"{key}_{horizon}")
    return out


def load_eval_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "neutral_score": float(cfg_get(config, "software_infrastructure_calibrated_scoring.neutral_score", 50.0)),
        "top_quantile": float(cfg_get(config, f"{CONFIG_KEY}.top_quantile", 0.20)),
        "min_positions": int(cfg_get(config, f"{CONFIG_KEY}.min_positions", 5)),
        "max_turnover": float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.60)),
        "max_top_cohort_share": float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.55)),
        "min_cross_section": int(cfg_get(config, "software_infrastructure_signal_diagnostics.min_cross_section", 30)),
        "stability_lambda": float(cfg_get(config, f"{CONFIG_KEY}.stability_lambda", 0.10)),
        "complexity_penalty_per_subfeature": float(cfg_get(config, f"{CONFIG_KEY}.complexity_penalty_per_subfeature", 0.0005)),
        "turnover_cost_bps": float(cfg_get(config, f"{CONFIG_KEY}.turnover_cost_bps", 20.0)),
    }


DEFAULT_GATE_HORIZONS = (21, 63)


def holdout_gate_value(config: dict[str, Any], gate_prefix: str, horizon: int, default: float) -> float:
    """Read a horizon-suffixed promotion-gate threshold from config.

    The historical literals (``*_21``/``*_63``) remain the fallback defaults,
    but only for the original (21, 63) horizons. If the configured diagnostics
    horizons change, the matching gate key must be added to config explicitly.
    """
    key = f"{CONFIG_KEY}.{gate_prefix}_{int(horizon)}"
    value = cfg_get(config, key, None)
    if value is None:
        if int(horizon) in DEFAULT_GATE_HORIZONS:
            return float(default)
        raise RuntimeError(
            f"Missing promotion gate config key '{key}' for configured horizon {horizon}; "
            "add it when changing software_infrastructure_signal_diagnostics.horizons_trading_days."
        )
    return float(value)


INFEASIBLE_OBJECTIVE_PENALTY = 1.0


def optimize_weights(
    panel: list[dict[str, Any]],
    train_dates: list[date],
    horizons: list[int],
    config: dict[str, Any],
    bounds: dict[str, tuple[float, float]],
    eval_kwargs: dict[str, Any],
    *,
    n_trials: int,
    seed: int,
    timeout_sec: int | None = None,
    storage_url: str | None = None,
    study_name: str | None = None,
    max_top_cohort_share_override: float | None = None,
) -> tuple[Candidate, Any]:
    import optuna

    hard_constraints = bool(cfg_get(config, f"{CONFIG_KEY}.hard_constraints_in_search", True))
    max_turnover = float(eval_kwargs["max_turnover"])
    configured_cohort_cap = float(eval_kwargs["max_top_cohort_share"])
    max_top_cohort_share = (
        configured_cohort_cap
        if max_top_cohort_share_override is None
        else float(max_top_cohort_share_override)
    )
    if not 0.0 <= max_top_cohort_share <= 1.0:
        raise ValueError("Effective max_top_cohort_share must be between 0 and 1")

    def objective(trial: Any) -> float:
        candidate = sample_candidate(trial, config, bounds)
        metrics = evaluate_candidate(panel, train_dates, horizons, candidate, **eval_kwargs)
        feasible = (
            float(metrics["avg_top_turnover"]) <= max_turnover
            and float(metrics["avg_top_cohort_share"]) <= max_top_cohort_share
        )
        trial.set_user_attr("metrics", {key: value for key, value in metrics.items() if key != "date_rows"})
        trial.set_user_attr("weights", json_ready_weights(candidate))
        trial.set_user_attr("feasible", int(feasible))
        trial.set_user_attr("effective_max_top_cohort_share", max_top_cohort_share)
        if hard_constraints and not feasible:
            return float(metrics["objective"]) - INFEASIBLE_OBJECTIVE_PENALTY
        return float(metrics["objective"])

    sampler = optuna.samplers.TPESampler(seed=seed)
    if storage_url and study_name:
        study = optuna.create_study(direction="maximize", sampler=sampler, study_name=study_name, storage=storage_url)
    else:
        study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=n_trials, timeout=timeout_sec, show_progress_bar=False)
    complete = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    if not complete:
        raise RuntimeError("All Stage 8B trials failed; no candidate available.")
    best_trial = study.best_trial
    if hard_constraints and not int(best_trial.user_attrs.get("feasible", 1)):
        raise RuntimeError(f"No feasible candidate found over {len(complete)} trials; constraints may be infeasible.")
    weights_raw = best_trial.user_attrs.get("weights", {})
    if isinstance(weights_raw, dict):
        return Candidate(
            component_weights={str(k): float(v) for k, v in weights_raw.get("component_weights", {}).items()},
            subfeature_specs={
                str(component): [(str(key), float(weight)) for key, weight in weights.items()]
                for component, weights in weights_raw.get("subfeature_weights", {}).items()
            },
        ), study
    return sample_candidate(best_trial, config, bounds), study


def current_candidate_scores(config: dict[str, Any], db_path: Path, candidate: Candidate, neutral_score: float) -> list[dict[str, Any]]:
    import sqlite3

    baseline_source = str(cfg_get(config, f"{CONFIG_KEY}.baseline_feature_source_id", "software_infrastructure_scoring_contract"))
    stage7_source = str(cfg_get(config, f"{CONFIG_KEY}.stage7_source_id", "software_infrastructure_calibrated_score_v1"))
    model_family = str(cfg_get(config, f"{CONFIG_KEY}.model_family", "software_infrastructure"))
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM feature_scoring_input
            WHERE source_id = ? AND model_family = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_input
                  WHERE source_id = ? AND model_family = ?
              )
            ORDER BY ticker
            """,
            (baseline_source, model_family, baseline_source, model_family),
        ).fetchall()
        stage7_rows = conn.execute(
            """
            SELECT ticker, final_rank, final_score
            FROM feature_scoring_model_output
            WHERE source_id = ? AND model_family = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_model_output
                  WHERE source_id = ? AND model_family = ?
              )
            """,
            (stage7_source, model_family, stage7_source, model_family),
        ).fetchall()
    finally:
        conn.close()
    out_rows = [dict(row) for row in rows]
    for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
        scores = percentile_scores(out_rows, raw_key, higher_is_better=higher_is_better, valid=valid)
        for row in out_rows:
            row[score_key] = scores.get(str(row["ticker"]))
    stage7_by_ticker = {str(row["ticker"]): dict(row) for row in stage7_rows}
    scored: list[dict[str, Any]] = []
    for row in out_rows:
        core_score, quality, component_scores, component_quality = score_row(row, candidate, neutral_score=neutral_score)
        scored.append(
            {
                "ticker": row["ticker"],
                "asof_date": row["asof_date"],
                "stage8_candidate_score": core_score,
                "stage8_quality": quality,
                "stage7_rank": stage7_by_ticker.get(str(row["ticker"]), {}).get("final_rank"),
                "stage7_score": stage7_by_ticker.get(str(row["ticker"]), {}).get("final_score"),
                "baseline_rank_ready_flag": row.get("rank_ready_flag"),
                "component_scores_json": json.dumps(component_scores, sort_keys=True),
                "component_quality_json": json.dumps(component_quality, sort_keys=True),
            }
        )
    rankable = sorted(
        [row for row in scored if int(row.get("baseline_rank_ready_flag") or 0) == 1],
        key=lambda row: (-float(row["stage8_candidate_score"]), str(row["ticker"])),
    )
    for idx, row in enumerate(rankable, start=1):
        row["stage8_candidate_rank"] = idx
    for row in scored:
        row.setdefault("stage8_candidate_rank", "")
    return sorted(scored, key=lambda row: (row["stage8_candidate_rank"] == "", row["stage8_candidate_rank"] or 10**9, row["ticker"]))


def run_software_infrastructure_optuna_calibration() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args("Run Stage 8B constrained Optuna calibration for software infrastructure.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    n_trials = int(args.n_trials if args.n_trials is not None else cfg_get(config, f"{CONFIG_KEY}.n_trials", 180))
    timeout_cfg = int(args.timeout_sec if args.timeout_sec is not None else cfg_get(config, f"{CONFIG_KEY}.timeout_sec", 0))
    timeout_sec = timeout_cfg if timeout_cfg > 0 else None
    seed = int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357))
    eval_kwargs = load_eval_kwargs(config)
    bounds = component_bounds(config)

    panel, panel_dates, horizons = load_panel(config, base_dir)
    panel, panel_dates, lock_provenance = cap_panel_to_train_end(
        panel,
        panel_dates,
        horizons,
        config,
        allow_post_lock=bool(args.allow_post_lock_panel),
    )
    train_dates, holdout_dates = split_dates(config, panel_dates, horizons)
    if len(train_dates) < 40 or len(holdout_dates) < 12:
        raise RuntimeError(f"Insufficient panel dates for Stage 8B: train={len(train_dates)} holdout={len(holdout_dates)}")
    LOGGER.info("Stage 8B split: train_dates=%d holdout_dates=%d", len(train_dates), len(holdout_dates))

    stage7 = stage7_candidate(config)
    stage7_train = evaluate_candidate(panel, train_dates, horizons, stage7, **eval_kwargs)
    stage7_holdout = evaluate_candidate(panel, holdout_dates, horizons, stage7, emit_date_rows=True, **eval_kwargs)

    study_name = f"software_stage8b_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    storage_path = output_dir / "stage8_optuna_study.sqlite"
    best_candidate, study = optimize_weights(
        panel,
        train_dates,
        horizons,
        config,
        bounds,
        eval_kwargs,
        n_trials=n_trials,
        seed=seed,
        timeout_sec=timeout_sec,
        storage_url=f"sqlite:///{storage_path.as_posix()}",
        study_name=study_name,
    )
    best_train = evaluate_candidate(panel, train_dates, horizons, best_candidate, emit_date_rows=True, **eval_kwargs)
    best_holdout = evaluate_candidate(panel, holdout_dates, horizons, best_candidate, emit_date_rows=True, **eval_kwargs)
    stage7_full = evaluate_candidate(panel, panel_dates, horizons, stage7, emit_date_rows=True, **eval_kwargs)

    folds = contiguous_folds(panel_dates, int(cfg_get(config, f"{CONFIG_KEY}.robustness_folds", 5)))
    fold_rows: list[dict[str, Any]] = []
    fold_wins = 0
    scored_folds = 0
    for fold_idx, fold_dates in enumerate(folds):
        if len(fold_dates) < 4:
            continue
        stage7_fold = evaluate_candidate(panel, fold_dates, horizons, stage7, **eval_kwargs)
        best_fold = evaluate_candidate(panel, fold_dates, horizons, best_candidate, **eval_kwargs)
        win = int(float(best_fold.get("objective", 0.0)) > float(stage7_fold.get("objective", 0.0)))
        fold_wins += win
        scored_folds += 1
        fold_rows.append(
            {
                "fold": fold_idx,
                "fold_start": fold_dates[0].isoformat(),
                "fold_end": fold_dates[-1].isoformat(),
                "n_dates": len(fold_dates),
                "stage7_objective": stage7_fold.get("objective"),
                "stage8_objective": best_fold.get("objective"),
                "stage8_wins": win,
                **{f"stage7_mean_ic_{h}": stage7_fold.get(f"mean_ic_{h}") for h in horizons},
                **{f"stage8_mean_ic_{h}": best_fold.get(f"mean_ic_{h}") for h in horizons},
            }
        )
    fold_win_fraction = fold_wins / scored_folds if scored_folds else 0.0

    trial_rows: list[dict[str, Any]] = []
    for trial in study.trials:
        metrics = trial.user_attrs.get("metrics", {})
        weights = trial.user_attrs.get("weights", {})
        trial_rows.append(
            {
                "trial": trial.number,
                "value": trial.value,
                "state": str(trial.state),
                "feasible": trial.user_attrs.get("feasible", ""),
                "effective_max_top_cohort_share": trial.user_attrs.get(
                    "effective_max_top_cohort_share", eval_kwargs["max_top_cohort_share"]
                ),
                **flatten_metrics("train", metrics, horizons),
                "component_weights_json": json.dumps(weights.get("component_weights", {}), sort_keys=True),
                "subfeature_weights_json": json.dumps(weights.get("subfeature_weights", {}), sort_keys=True),
            }
        )

    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    min_ic_primary = holdout_gate_value(config, "min_holdout_mean_ic", primary, 0.005)
    min_ic_secondary = holdout_gate_value(config, "min_holdout_mean_ic", secondary, 0.005)
    min_hit = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_hit_rate", 0.50))
    min_improvement = float(cfg_get(config, f"{CONFIG_KEY}.promotion_min_objective_improvement", 0.002))
    min_fold_win_fraction = float(cfg_get(config, f"{CONFIG_KEY}.min_fold_win_fraction", 0.50))
    min_spread_primary = holdout_gate_value(config, "min_holdout_mean_spread_net", primary, 0.0)
    min_spread_secondary = holdout_gate_value(config, "min_holdout_mean_spread_net", secondary, 0.0)
    promotion_candidate = int(
        float(best_holdout.get("objective", 0.0)) >= float(stage7_holdout.get("objective", 0.0)) + min_improvement
        and float(best_holdout.get(f"mean_ic_{primary}", 0.0)) >= min_ic_primary
        and float(best_holdout.get(f"mean_ic_{secondary}", 0.0)) >= min_ic_secondary
        and float(best_holdout.get(f"hit_rate_{primary}", 0.0)) >= min_hit
        and float(best_holdout.get(f"mean_spread_net_{primary}", 0.0)) >= min_spread_primary
        and float(best_holdout.get(f"mean_spread_net_{secondary}", 0.0)) >= min_spread_secondary
        and float(best_holdout.get("avg_top_turnover", 1.0)) <= eval_kwargs["max_turnover"]
        and float(best_holdout.get("avg_top_cohort_share", 1.0)) <= eval_kwargs["max_top_cohort_share"]
        and fold_win_fraction >= min_fold_win_fraction
    )

    summary_rows = [
        {
            "model": "stage7_baseline",
            **flatten_metrics("train", stage7_train, horizons),
            **flatten_metrics("holdout", stage7_holdout, horizons),
            "fold_win_fraction": "",
            "promotion_candidate": 0,
        },
        {
            "model": "stage8_best_candidate",
            **flatten_metrics("train", best_train, horizons),
            **flatten_metrics("holdout", best_holdout, horizons),
            "fold_win_fraction": round(fold_win_fraction, 4),
            "promotion_candidate": promotion_candidate,
        },
    ]
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except OSError:
        git_commit = ""

    best_weights = {
        "source_id": str(cfg_get(config, f"{CONFIG_KEY}.source_id", "software_infrastructure_stage8_optuna_calibration")),
        "model_family": str(cfg_get(config, f"{CONFIG_KEY}.model_family", "software_infrastructure")),
        "n_trials": len(study.trials),
        "train_dates": [train_dates[0].isoformat(), train_dates[-1].isoformat()],
        "holdout_dates": [holdout_dates[0].isoformat(), holdout_dates[-1].isoformat()],
        "promotion_candidate": promotion_candidate,
        "stage7_holdout_objective": stage7_holdout.get("objective"),
        "stage8_holdout_objective": best_holdout.get("objective"),
        "objective_improvement": float(best_holdout.get("objective", 0.0)) - float(stage7_holdout.get("objective", 0.0)),
        "fold_win_fraction": fold_win_fraction,
        "robustness_folds": scored_folds,
        "random_seed": seed,
        "study_name": study_name,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "git_commit": git_commit,
        **lock_provenance,
        "objective_params": {
            "stability_lambda": eval_kwargs["stability_lambda"],
            "complexity_penalty_per_subfeature": eval_kwargs["complexity_penalty_per_subfeature"],
            "turnover_cost_bps": eval_kwargs["turnover_cost_bps"],
            "hard_constraints_in_search": bool(cfg_get(config, f"{CONFIG_KEY}.hard_constraints_in_search", True)),
            "min_holdout_mean_spread_net_primary": min_spread_primary,
            "min_holdout_mean_spread_net_secondary": min_spread_secondary,
        },
        **json_ready_weights(best_candidate),
    }

    write_csv(output_dir / "stage8_trials.csv", trial_rows)
    write_csv(output_dir / "stage8_best_summary.csv", summary_rows)
    write_csv(output_dir / "stage8_best_train_by_date.csv", best_train["date_rows"])
    write_csv(output_dir / "stage8_best_holdout_by_date.csv", best_holdout["date_rows"])
    write_csv(output_dir / "stage8_stage7_holdout_by_date.csv", stage7_holdout["date_rows"])
    write_csv(output_dir / "stage8_stage7_full_by_date.csv", stage7_full["date_rows"])
    write_csv(output_dir / "stage8_fold_robustness.csv", fold_rows)
    write_csv(output_dir / "stage8_candidate_current_scores.csv", current_candidate_scores(config, db_path, best_candidate, eval_kwargs["neutral_score"]))
    (output_dir / "stage8_best_weights.json").write_text(json.dumps(best_weights, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info(
        "Stage 8B complete: stage7_holdout=%s stage8_holdout=%s improvement=%.5f fold_win=%.2f promoted=%s output=%s",
        stage7_holdout.get("objective"),
        best_holdout.get("objective"),
        best_weights["objective_improvement"],
        fold_win_fraction,
        promotion_candidate,
        output_dir,
    )


def validate_software_infrastructure_optuna_calibration() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args("Validate Stage 8B constrained Optuna calibration outputs for software infrastructure.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    errors: list[str] = []
    required = [
        output_dir / "stage8_trials.csv",
        output_dir / "stage8_best_summary.csv",
        output_dir / "stage8_best_weights.json",
        output_dir / "stage8_best_holdout_by_date.csv",
        output_dir / "stage8_fold_robustness.csv",
        output_dir / "stage8_candidate_current_scores.csv",
    ]
    for path in required:
        if not path.exists() or path.stat().st_size == 0:
            errors.append(f"Missing or empty Stage 8B output: {path}")
    best: dict[str, Any] = {}
    if (output_dir / "stage8_best_weights.json").exists():
        best = json.loads((output_dir / "stage8_best_weights.json").read_text(encoding="utf-8"))
        if int(best.get("n_trials") or 0) < 20:
            errors.append(f"Stage 8B trial count too low: {best.get('n_trials')}")
        weights = best.get("component_weights", {})
        bounds = component_bounds(config)
        if isinstance(weights, dict):
            total = sum(float(value) for value in weights.values())
            if abs(total - 1.0) > 0.0001:
                errors.append(f"Component weights do not sum to 1.0: {total}")
            for component, (lo, hi) in bounds.items():
                value = float(weights.get(component, 0.0))
                if value < lo - 0.0001 or value > hi + 0.0001:
                    errors.append(f"Component weight outside bounds: {component}={value} expected [{lo}, {hi}]")
        effective_weights = best.get("effective_subfeature_weights", {})
        for (component, score_key), (lo, hi) in subfeature_effective_bounds(config).items():
            value = 0.0
            if isinstance(effective_weights, dict) and isinstance(effective_weights.get(component), dict):
                value = float(effective_weights.get(component, {}).get(score_key, 0.0))
            if value < lo - 0.0001 or value > hi + 0.0001:
                errors.append(f"Effective subfeature weight outside bounds: {component}.{score_key}={value} expected [{lo}, {hi}]")
    summary_path = output_dir / "stage8_best_summary.csv"
    summary_rows: list[dict[str, str]] = []
    if summary_path.exists():
        with summary_path.open("r", encoding="utf-8", newline="") as handle:
            summary_rows = list(csv.DictReader(handle))
    if len(summary_rows) < 2:
        errors.append("Stage 8B summary must include Stage 7 baseline and Stage 8 candidate rows.")
    else:
        candidate = next((row for row in summary_rows if row.get("model") == "stage8_best_candidate"), {})
        promotion_candidate = int(float(candidate.get("promotion_candidate") or 0))
        if promotion_candidate:
            horizons = [int(value) for value in cfg_get(config, "software_infrastructure_signal_diagnostics.horizons_trading_days", [21, 63])]
            primary = horizons[0]
            secondary = horizons[1] if len(horizons) > 1 else horizons[0]
            max_turnover = float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.60))
            max_cohort = float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.55))
            min_spread_primary = holdout_gate_value(config, "min_holdout_mean_spread_net", primary, 0.0)
            min_spread_secondary = holdout_gate_value(config, "min_holdout_mean_spread_net", secondary, 0.0)
            if float(candidate.get("holdout_avg_top_turnover") or 1.0) > max_turnover + 0.0001:
                errors.append(f"Promoted candidate exceeds turnover cap: {candidate.get('holdout_avg_top_turnover')}")
            if float(candidate.get("holdout_avg_top_cohort_share") or 1.0) > max_cohort + 0.0001:
                errors.append(f"Promoted candidate exceeds cohort cap: {candidate.get('holdout_avg_top_cohort_share')}")
            if float(candidate.get(f"holdout_mean_spread_net_{primary}") or 0.0) < min_spread_primary - 0.0001:
                errors.append(f"Promoted candidate fails {primary}d spread gate: {candidate.get(f'holdout_mean_spread_net_{primary}')}")
            if float(candidate.get(f"holdout_mean_spread_net_{secondary}") or 0.0) < min_spread_secondary - 0.0001:
                errors.append(f"Promoted candidate fails {secondary}d spread gate: {candidate.get(f'holdout_mean_spread_net_{secondary}')}")
        else:
            LOGGER.info("Stage 8B candidate is report-only and not promoted.")
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info("Stage 8B Optuna outputs validated: %s", output_dir)
    return 0


def run_software_infrastructure_walk_forward_calibration() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    try:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        pass
    args = parse_args("Run Stage 8C walk-forward refit validation for software infrastructure.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    base_output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    output_dir = base_output_dir / str(cfg_get(config, f"{CONFIG_KEY}.walk_forward.output_subdir", "walk_forward"))
    output_dir.mkdir(parents=True, exist_ok=True)
    n_trials = int(args.n_trials if args.n_trials is not None else cfg_get(config, f"{CONFIG_KEY}.walk_forward.n_trials_per_refit", 60))
    timeout_cfg = int(args.timeout_sec if args.timeout_sec is not None else cfg_get(config, f"{CONFIG_KEY}.timeout_sec", 0))
    timeout_sec = timeout_cfg if timeout_cfg > 0 else None
    seed = int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357))
    initial_train = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.initial_train_dates", 40))
    block_size = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.test_block_dates", 12))
    min_blocks = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.min_test_blocks", 3))
    step = int(cfg_get(config, "software_infrastructure_signal_diagnostics.step_trading_days", 21))
    eval_kwargs = load_eval_kwargs(config)
    bounds = component_bounds(config)

    panel, panel_dates, horizons = load_panel(config, base_dir)
    panel, panel_dates, lock_provenance = cap_panel_to_train_end(
        panel,
        panel_dates,
        horizons,
        config,
        allow_post_lock=bool(args.allow_post_lock_panel),
    )
    min_embargo = int(math.ceil(max(horizons) / max(1, step))) + 1
    embargo = max(min_embargo, int(cfg_get(config, f"{CONFIG_KEY}.embargo_panel_dates", 4)))
    stage7 = stage7_candidate(config)
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    min_ic_primary = holdout_gate_value(config, "min_holdout_mean_ic", primary, 0.005)
    min_ic_secondary = holdout_gate_value(config, "min_holdout_mean_ic", secondary, 0.005)
    min_hit = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_hit_rate", 0.50))
    min_spread_primary = holdout_gate_value(config, "min_holdout_mean_spread_net", primary, 0.0)
    min_spread_secondary = holdout_gate_value(config, "min_holdout_mean_spread_net", secondary, 0.0)
    min_fold_win_fraction = float(cfg_get(config, f"{CONFIG_KEY}.min_fold_win_fraction", 0.50))
    cohort_cap_tolerance = float(
        cfg_get(config, f"{CONFIG_KEY}.walk_forward.max_top_cohort_share_incumbent_tolerance", 0.02)
    )

    block_rows: list[dict[str, Any]] = []
    improvements: list[float] = []
    refit_primary_ics: list[float] = []
    stage7_primary_ics: list[float] = []
    refit_secondary_ics: list[float] = []
    stage7_secondary_ics: list[float] = []
    refit_primary_spreads: list[float] = []
    refit_secondary_spreads: list[float] = []
    refit_primary_hits: list[float] = []
    refit_constraint_passes = 0
    gate_passes = 0
    wins = 0
    block_idx = 0
    test_start = initial_train + embargo
    while test_start < len(panel_dates):
        train_dates = panel_dates[: test_start - embargo]
        test_dates = panel_dates[test_start: test_start + block_size]
        if len(test_dates) < 4 or len(train_dates) < 20:
            break
        stage7_train_metrics = evaluate_candidate(panel, train_dates, horizons, stage7, **eval_kwargs)
        effective_search_cohort_cap = incumbent_relative_cohort_cap(
            float(eval_kwargs["max_top_cohort_share"]),
            float(stage7_train_metrics["avg_top_cohort_share"]),
            cohort_cap_tolerance,
        )
        candidate, _study = optimize_weights(
            panel,
            train_dates,
            horizons,
            config,
            bounds,
            eval_kwargs,
            n_trials=n_trials,
            seed=seed + block_idx,
            timeout_sec=timeout_sec,
            max_top_cohort_share_override=effective_search_cohort_cap,
        )
        refit_metrics = evaluate_candidate(panel, test_dates, horizons, candidate, **eval_kwargs)
        stage7_metrics = evaluate_candidate(panel, test_dates, horizons, stage7, **eval_kwargs)
        effective_test_cohort_cap = incumbent_relative_cohort_cap(
            float(eval_kwargs["max_top_cohort_share"]),
            float(stage7_metrics["avg_top_cohort_share"]),
            cohort_cap_tolerance,
        )
        improvement = float(refit_metrics.get("objective", 0.0)) - float(stage7_metrics.get("objective", 0.0))
        win = int(improvement > 0)
        constraint_pass = int(
            float(refit_metrics.get("avg_top_turnover", 1.0)) <= eval_kwargs["max_turnover"]
            and float(refit_metrics.get("avg_top_cohort_share", 1.0)) <= effective_test_cohort_cap
        )
        gate_pass = int(
            win
            and constraint_pass
            and float(refit_metrics.get(f"mean_ic_{primary}", 0.0)) >= min_ic_primary
            and float(refit_metrics.get(f"mean_ic_{secondary}", 0.0)) >= min_ic_secondary
            and float(refit_metrics.get(f"hit_rate_{primary}", 0.0)) >= min_hit
            and float(refit_metrics.get(f"mean_spread_net_{primary}", 0.0)) >= min_spread_primary
            and float(refit_metrics.get(f"mean_spread_net_{secondary}", 0.0)) >= min_spread_secondary
        )
        wins += win
        refit_constraint_passes += constraint_pass
        gate_passes += gate_pass
        improvements.append(improvement)
        refit_primary_ics.append(float(refit_metrics.get(f"mean_ic_{primary}", 0.0)))
        stage7_primary_ics.append(float(stage7_metrics.get(f"mean_ic_{primary}", 0.0)))
        refit_secondary_ics.append(float(refit_metrics.get(f"mean_ic_{secondary}", 0.0)))
        stage7_secondary_ics.append(float(stage7_metrics.get(f"mean_ic_{secondary}", 0.0)))
        refit_primary_spreads.append(float(refit_metrics.get(f"mean_spread_net_{primary}", 0.0)))
        refit_secondary_spreads.append(float(refit_metrics.get(f"mean_spread_net_{secondary}", 0.0)))
        refit_primary_hits.append(float(refit_metrics.get(f"hit_rate_{primary}", 0.0)))
        block_rows.append(
            {
                "block": block_idx,
                "train_start": train_dates[0].isoformat(),
                "train_end": train_dates[-1].isoformat(),
                "test_start": test_dates[0].isoformat(),
                "test_end": test_dates[-1].isoformat(),
                "n_train_dates": len(train_dates),
                "n_test_dates": len(test_dates),
                "refit_objective": refit_metrics.get("objective"),
                "stage7_objective": stage7_metrics.get("objective"),
                "objective_improvement": improvement,
                "refit_wins": win,
                "constraint_pass": constraint_pass,
                "promotion_gate_pass": gate_pass,
                **{f"refit_mean_ic_{h}": refit_metrics.get(f"mean_ic_{h}") for h in horizons},
                **{f"stage7_mean_ic_{h}": stage7_metrics.get(f"mean_ic_{h}") for h in horizons},
                **{f"refit_hit_rate_{h}": refit_metrics.get(f"hit_rate_{h}") for h in horizons},
                **{f"refit_mean_spread_net_{h}": refit_metrics.get(f"mean_spread_net_{h}") for h in horizons},
                "refit_avg_top_turnover": refit_metrics.get("avg_top_turnover"),
                "refit_avg_top_cohort_share": refit_metrics.get("avg_top_cohort_share"),
                "stage7_avg_top_cohort_share": stage7_metrics.get("avg_top_cohort_share"),
                "configured_max_top_cohort_share": eval_kwargs["max_top_cohort_share"],
                "effective_search_max_top_cohort_share": effective_search_cohort_cap,
                "effective_max_top_cohort_share": effective_test_cohort_cap,
                "component_weights_json": json.dumps(candidate.component_weights, sort_keys=True),
            }
        )
        LOGGER.info(
            "Walk-forward block %d: train=%s..%s test=%s..%s improvement=%.5f win=%d gate=%d",
            block_idx,
            train_dates[0],
            train_dates[-1],
            test_dates[0],
            test_dates[-1],
            improvement,
            win,
            gate_pass,
        )
        test_start += block_size
        block_idx += 1

    if len(block_rows) < min_blocks:
        raise RuntimeError(
            f"Only {len(block_rows)} walk-forward blocks available (need {min_blocks}); "
            "extend panel history or reduce walk_forward.test_block_dates."
        )

    improvement_stats = stats(improvements)
    paired_t = improvement_stats["t_stat"]
    win_rate = wins / len(block_rows)
    gate_pass_rate = gate_passes / len(block_rows)
    mean_primary_spread = sum(refit_primary_spreads) / len(refit_primary_spreads)
    mean_secondary_spread = sum(refit_secondary_spreads) / len(refit_secondary_spreads)
    mean_primary_ic = sum(refit_primary_ics) / len(refit_primary_ics)
    mean_secondary_ic = sum(refit_secondary_ics) / len(refit_secondary_ics)
    mean_primary_hit = sum(refit_primary_hits) / len(refit_primary_hits)
    constraint_pass_rate = refit_constraint_passes / len(block_rows)
    procedure_adds_value = int(
        win_rate >= min_fold_win_fraction
        and improvement_stats["mean"] > 0
        and mean_primary_ic >= min_ic_primary
        and mean_secondary_ic >= min_ic_secondary
        and mean_primary_hit >= min_hit
        and mean_primary_spread >= min_spread_primary
        and mean_secondary_spread >= min_spread_secondary
        and constraint_pass_rate >= min_fold_win_fraction
    )
    summary = {
        "source_id": "software_infrastructure_stage8_walk_forward_calibration",
        "n_blocks": len(block_rows),
        "n_trials_per_refit": n_trials,
        "initial_train_dates": initial_train,
        "test_block_dates": block_size,
        "embargo_panel_dates": embargo,
        "configured_max_top_cohort_share": eval_kwargs["max_top_cohort_share"],
        "max_top_cohort_share_incumbent_tolerance": cohort_cap_tolerance,
        "adaptive_cohort_cap_policy": "max_configured_or_incumbent_plus_tolerance",
        "refit_win_rate": win_rate,
        "promotion_gate_pass_rate": gate_pass_rate,
        "constraint_pass_rate": constraint_pass_rate,
        "mean_objective_improvement": improvement_stats["mean"],
        "improvement_paired_t": paired_t,
        f"mean_refit_oos_ic_{primary}": mean_primary_ic,
        f"mean_stage7_oos_ic_{primary}": sum(stage7_primary_ics) / len(stage7_primary_ics),
        f"mean_refit_oos_ic_{secondary}": mean_secondary_ic,
        f"mean_stage7_oos_ic_{secondary}": sum(stage7_secondary_ics) / len(stage7_secondary_ics),
        f"mean_refit_hit_rate_{primary}": mean_primary_hit,
        f"mean_refit_spread_net_{primary}": mean_primary_spread,
        f"mean_refit_spread_net_{secondary}": mean_secondary_spread,
        "procedure_adds_value": procedure_adds_value,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "random_seed": seed,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **lock_provenance,
    }
    write_csv(output_dir / "walk_forward_blocks.csv", block_rows)
    write_csv(output_dir / "walk_forward_summary.csv", [summary])
    (output_dir / "walk_forward_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info(
        "Stage 8C walk-forward complete: blocks=%d win_rate=%.2f mean_improvement=%.5f gate_pass_rate=%.2f procedure_adds_value=%s output=%s",
        len(block_rows),
        win_rate,
        improvement_stats["mean"],
        gate_pass_rate,
        procedure_adds_value,
        output_dir,
    )


def validate_software_infrastructure_walk_forward_calibration() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args("Validate Stage 8C walk-forward calibration outputs for software infrastructure.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    base_output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    output_dir = base_output_dir / str(cfg_get(config, f"{CONFIG_KEY}.walk_forward.output_subdir", "walk_forward"))
    errors: list[str] = []
    required = [
        output_dir / "walk_forward_blocks.csv",
        output_dir / "walk_forward_summary.csv",
        output_dir / "walk_forward_summary.json",
    ]
    for path in required:
        if not path.exists() or path.stat().st_size == 0:
            errors.append(f"Missing or empty Stage 8C output: {path}")
    summary: dict[str, Any] = {}
    summary_path = output_dir / "walk_forward_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid walk_forward_summary.json: {exc}")
    min_blocks = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.min_test_blocks", 3))
    horizons = [int(value) for value in cfg_get(config, "software_infrastructure_signal_diagnostics.horizons_trading_days", [21, 63])]
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    if summary:
        if int(summary.get("n_blocks") or 0) < min_blocks:
            errors.append(f"Too few walk-forward blocks: {summary.get('n_blocks')} < {min_blocks}")
        if int(summary.get("procedure_adds_value") or 0):
            min_spread_primary = holdout_gate_value(config, "min_holdout_mean_spread_net", primary, 0.0)
            min_spread_secondary = holdout_gate_value(config, "min_holdout_mean_spread_net", secondary, 0.0)
            if float(summary.get(f"mean_refit_spread_net_{primary}") or 0.0) < min_spread_primary - 0.0001:
                errors.append(f"procedure_adds_value=1 but {primary}d spread gate failed")
            if float(summary.get(f"mean_refit_spread_net_{secondary}") or 0.0) < min_spread_secondary - 0.0001:
                errors.append(f"procedure_adds_value=1 but {secondary}d spread gate failed")
        else:
            LOGGER.info("Stage 8C procedure is report-only and not promotable.")
    blocks_path = output_dir / "walk_forward_blocks.csv"
    if blocks_path.exists():
        with blocks_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) < min_blocks:
            errors.append(f"walk_forward_blocks.csv has too few rows: {len(rows)}")
        missing_cols = {
            "objective_improvement",
            "promotion_gate_pass",
            f"refit_mean_spread_net_{primary}",
            f"refit_mean_spread_net_{secondary}",
        } - set(rows[0].keys() if rows else [])
        if missing_cols:
            errors.append(f"walk_forward_blocks.csv missing columns: {sorted(missing_cols)}")
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info("Stage 8C walk-forward outputs validated: %s", output_dir)
    return 0


if __name__ == "__main__":
    from technology.core.optuna_artifact_governance import (
        run_stage8_with_governance,
        run_walk_forward_with_governance,
        validate_stage8_from_argv,
        validate_walk_forward_from_argv,
    )

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        sys.argv.pop(1)
        raise SystemExit(max(validate_software_infrastructure_optuna_calibration(), validate_stage8_from_argv("software_infrastructure")))
    if command == "walk-forward":
        sys.argv.pop(1)
        run_walk_forward_with_governance(run_software_infrastructure_walk_forward_calibration, "software_infrastructure")
        raise SystemExit(0)
    if command == "validate-walk-forward":
        sys.argv.pop(1)
        raise SystemExit(max(validate_software_infrastructure_walk_forward_calibration(), validate_walk_forward_from_argv("software_infrastructure")))
    run_stage8_with_governance(run_software_infrastructure_optuna_calibration, "software_infrastructure")
