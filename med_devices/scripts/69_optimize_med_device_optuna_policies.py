#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

try:
    import optuna
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("optuna is required for script 69. Install optuna in the active environment.") from exc


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_EXCLUDED_COMPONENTS = {
    "raw_composite_score",
    "composite_score",
    "cohort_percentile",
    "composite_percentile",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
    "data_completeness_score",
    "liquidity_score",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "fda_event_risk_score",
    "fda_event_risk_breadth_adjusted_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
}
TEXT_FIELDS = {
    "asof_date",
    "scoring_model_version",
    "ticker",
    "company_name",
    "subsector",
    "classification",
    "decision_bucket",
    "entry_status",
    "calibration_cohort",
    "cohort_rank_bucket",
    "safe_core_status",
    "safe_core_reason",
    "tier1_safety_status",
    "tier1_safety_reason",
    "classification_reason",
}
TRIAL_FIELDS = [
    "calibration_cohort",
    "trial_number",
    "trial_state",
    "prune_reason",
    "candidate_id",
    "objective_value",
    "candidate_status",
    "candidate_reason",
    "fold_count",
    "pass_fold_count",
    "pass_fold_rate",
    "validation_count",
    "validation_unique_tickers",
    "validation_ticker_coverage",
    "mean_excess_hit_rate",
    "mean_loss_rate",
    "max_single_ticker_share",
    "max_single_ticker_share_limit",
    "min_lcb_scope",
    "mean_lcb_excess_60d",
    "min_lcb_excess_60d",
    "delta_lcb_vs_topdecile_60d",
    "delta_lcb_vs_production_60d",
    "mean_lcb_excess_120d",
    "min_lcb_excess_120d",
    "delta_lcb_vs_topdecile_120d",
    "delta_lcb_vs_production_120d",
    "mean_median_excess_60d",
    "mean_median_excess_120d",
    "component_count",
    "component_spec_json",
    "sleeve_weight_json",
    "gate_spec_json",
    "params_json",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "candidate_count",
    "completed_candidate_count",
    "pruned_candidate_count",
    "best_candidate_id",
    "best_objective_value",
    "best_candidate_status",
    "best_candidate_reason",
    "best_pass_fold_rate",
    "best_validation_unique_tickers",
    "best_min_lcb_excess_60d",
    "best_min_lcb_excess_120d",
    "best_mean_loss_rate",
    "best_max_single_ticker_share",
    "best_max_single_ticker_share_limit",
    "best_min_lcb_scope",
    "eligible_component_count",
    "eligible_components_json",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommended_candidate_id",
    "promotion_status",
    "objective_value",
    "pass_fold_count",
    "pass_fold_rate",
    "validation_count",
    "validation_unique_tickers",
    "validation_ticker_coverage",
    "mean_excess_hit_rate",
    "mean_loss_rate",
    "max_single_ticker_share",
    "max_single_ticker_share_limit",
    "min_lcb_scope",
    "mean_lcb_excess_60d",
    "min_lcb_excess_60d",
    "delta_lcb_vs_topdecile_60d",
    "mean_lcb_excess_120d",
    "min_lcb_excess_120d",
    "delta_lcb_vs_topdecile_120d",
    "promotion_reason",
    "component_spec_json",
    "sleeve_weight_json",
    "gate_spec_json",
]
FOLD_DIAGNOSTIC_FIELDS = [
    "calibration_cohort",
    "trial_number",
    "trial_state",
    "candidate_id",
    "fold_id",
    "is_incomplete_fold",
    "counted_in_pass_rate",
    "horizon_days",
    "validation_start",
    "validation_end",
    "selected_count",
    "unique_tickers",
    "ticker_coverage",
    "excess_hit_count",
    "excess_hit_rate",
    "excess_hit_p_value",
    "loss_rate",
    "lcb_excess",
    "single_ticker_share",
    "fold_horizon_status",
    "guardrail_reason",
    "selected_tickers",
]


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    validation_calendar_days: int
    is_incomplete: bool = False


@dataclass(frozen=True)
class PanelRow:
    idx: int
    asof_date: date
    ticker: str
    cohort: str
    cohort_rank_bucket: str
    classification: str
    features: dict[str, float]


@dataclass(frozen=True)
class ComponentCandidate:
    component: str
    direction: str
    quality: float
    sleeve: str
    horizons: tuple[int, ...]
    ic: float
    spread: float
    q_value: float


@dataclass(frozen=True)
class TrialCandidate:
    cohort: str
    components: tuple[tuple[str, str, float, str], ...]
    gates: dict[str, float]
    candidate_id: str


def candidate_to_attr(candidate: TrialCandidate) -> dict[str, Any]:
    return {
        "cohort": candidate.cohort,
        "components": [
            {
                "component": component,
                "direction": direction,
                "weight": weight,
                "sleeve": sleeve,
            }
            for component, direction, weight, sleeve in candidate.components
        ],
        "gates": dict(candidate.gates),
        "candidate_id": candidate.candidate_id,
    }


def candidate_from_attr(raw: object) -> TrialCandidate | None:
    if isinstance(raw, TrialCandidate):
        return raw
    if not isinstance(raw, dict):
        return None
    components: list[tuple[str, str, float, str]] = []
    for item in raw.get("components") or []:
        if not isinstance(item, dict):
            continue
        component = str(item.get("component") or "").strip()
        direction = str(item.get("direction") or "").strip()
        sleeve = str(item.get("sleeve") or "").strip()
        if not component or not direction:
            continue
        components.append((component, direction, float(item.get("weight") or 0.0), sleeve))
    gates_raw = raw.get("gates") if isinstance(raw.get("gates"), dict) else {}
    gates = {str(key): float(value or 0.0) for key, value in gates_raw.items()}
    cohort = str(raw.get("cohort") or "").strip()
    candidate_id = str(raw.get("candidate_id") or "").strip()
    if not cohort or not candidate_id:
        return None
    return TrialCandidate(cohort=cohort, components=tuple(components), gates=gates, candidate_id=candidate_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run constrained Optuna optimization for med-device shadow cohort policies. "
            "This writes calibration artifacts only; it does not promote production config."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--policy-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--fold-diagnostics-csv", type=Path, default=None)
    parser.add_argument("--config-fragment-yaml", type=Path, default=None)
    parser.add_argument("--cohorts", type=str, default="")
    parser.add_argument("--horizons", type=str, default="")
    parser.add_argument("--n-trials-per-cohort", type=int, default=None)
    parser.add_argument("--timeout-sec-per-cohort", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def finite_float_values(values: list[object]) -> list[float]:
    out: list[float] = []
    for raw in values:
        value = to_float(raw)
        if value is not None:
            out.append(value)
    return out


def score_or(row: PanelRow, field: str, default: float = 50.0) -> float:
    value = row.features.get(field)
    if value is None or not math.isfinite(value):
        return default
    return max(0.0, min(100.0, value))


def parse_date(raw: object) -> date:
    return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return date(year, month, min(value.day, days_in_month))


def parse_int_list(raw: object) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            out.append(int(float(text)))
        except ValueError:
            continue
    return out


def parse_float_list(raw: object, default: list[float]) -> list[float]:
    out: list[float] = []
    for item in str(raw or "").split(","):
        value = to_float(item)
        if value is not None:
            out.append(value)
    return out or list(default)


def parse_str_set(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def cfg_int(config: dict[str, Any], path: str, default: int) -> int:
    value = to_float(cfg_get(config, path, default))
    return int(value if value is not None else default)


def cfg_float(config: dict[str, Any], path: str, default: float) -> float:
    value = to_float(cfg_get(config, path, default))
    return float(value if value is not None else default)


def cfg_bool(config: dict[str, Any], path: str, default: bool) -> bool:
    raw = cfg_get(config, path, default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "none", ""}


def resolve_output_path(
    *,
    cli_value: Path | None,
    config: dict[str, Any],
    config_path: str,
    default: str,
    base_dir: Path,
) -> Path:
    if cli_value is not None:
        return cli_value if cli_value.is_absolute() else (Path.cwd() / cli_value).resolve()
    return resolve_path(cfg_get(config, config_path, default), base_dir=base_dir)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def lcb(values: list[float], z: float = 1.64) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def fmt(value: object) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.6f}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compact_reasons(reasons: list[str], *, max_reasons: int = 24) -> str:
    unique = list(dict.fromkeys(reason for reason in reasons if reason))
    if len(unique) <= max_reasons:
        return ";".join(unique)
    return ";".join(unique[:max_reasons] + [f"additional_reasons_{len(unique) - max_reasons}"])


def component_sleeve(component: str) -> str:
    if component.startswith(("borrow_", "short_", "institutional_", "insider_")):
        return "market_positioning"
    if component.startswith(("technical_", "momentum_", "realized_vol", "round_trip_cost")):
        return "technical"
    if component.startswith(("fda_", "regulatory_")):
        return "fda_regulatory"
    if component.startswith(("durable_growth", "fundamental_", "quality_value")):
        return "fundamental_quality"
    if component.startswith(("valuation_", "value_trap")):
        return "valuation"
    if component.startswith(("reimbursement_", "direct_code", "payment_rate", "coverage_policy")):
        return "reimbursement"
    if component.startswith(("sentiment_", "data_completeness", "liquidity")):
        return "supporting_quality"
    return "other"


def component_alias_preference(component: str) -> int:
    if component == "durable_growth_alpha_score":
        return 30
    if component == "durable_growth_score":
        return 20
    if component == "durable_growth_score_legacy":
        return 10
    if component.endswith("_score"):
        return 5
    return 0


def deduplicate_component_candidates(
    candidates: list[ComponentCandidate],
    *,
    enabled: bool,
) -> list[ComponentCandidate]:
    if not enabled:
        return sorted(candidates, key=lambda item: (item.quality, item.ic, item.spread, item.component), reverse=True)
    best_by_fingerprint: dict[tuple[Any, ...], ComponentCandidate] = {}
    for item in candidates:
        fingerprint = (
            item.direction,
            item.horizons,
            round(item.ic, 6),
            round(item.spread, 6),
            round(item.q_value, 6),
        )
        current = best_by_fingerprint.get(fingerprint)
        if current is None:
            best_by_fingerprint[fingerprint] = item
            continue
        current_key = (component_alias_preference(current.component), current.quality, current.component)
        item_key = (component_alias_preference(item.component), item.quality, item.component)
        if item_key > current_key:
            best_by_fingerprint[fingerprint] = item
    return sorted(
        best_by_fingerprint.values(),
        key=lambda item: (item.quality, item.ic, item.spread, component_alias_preference(item.component), item.component),
        reverse=True,
    )


def load_panel(path: Path) -> tuple[list[PanelRow], set[str]]:
    rows: list[PanelRow] = []
    feature_names: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for idx, raw in enumerate(reader):
            cohort = str(raw.get("calibration_cohort") or "").strip()
            ticker = str(raw.get("ticker") or "").strip()
            asof_raw = raw.get("asof_date")
            if not cohort or not ticker or not asof_raw:
                continue
            features: dict[str, float] = {}
            for key, value in raw.items():
                if key in TEXT_FIELDS:
                    continue
                number = to_float(value)
                if number is not None:
                    features[key] = number
                    feature_names.add(key)
            rows.append(
                PanelRow(
                    idx=idx,
                    asof_date=parse_date(asof_raw),
                    ticker=ticker,
                    cohort=cohort,
                    cohort_rank_bucket=str(raw.get("cohort_rank_bucket") or "").strip(),
                    classification=str(raw.get("classification") or "").strip(),
                    features=features,
                )
            )
    return rows, feature_names


def load_component_candidates(
    path: Path,
    *,
    config: dict[str, Any],
    feature_names: set[str],
    horizons: set[int],
) -> dict[str, list[ComponentCandidate]]:
    actions = parse_str_set(
        cfg_get(config, "calibration.optuna_policy_optimizer.review_actions", "promote_to_cohort_policy_review")
    )
    excluded = set(DEFAULT_EXCLUDED_COMPONENTS)
    excluded.update(parse_str_set(cfg_get(config, "calibration.component_promotion_review.excluded_components", "")))
    excluded.update(parse_str_set(cfg_get(config, "calibration.optuna_policy_optimizer.excluded_components", "")))
    require_persistence = cfg_bool(
        config,
        "calibration.optuna_policy_optimizer.require_60_120_persistence",
        cfg_bool(config, "calibration.component_promotion_review.require_60_120_persistence", True),
    )
    deduplicate_fingerprints = cfg_bool(
        config,
        "calibration.optuna_policy_optimizer.deduplicate_ic_fingerprints",
        True,
    )
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in read_csv(path):
        if str(row.get("review_action") or "").strip() not in actions:
            continue
        component = str(row.get("component") or "").strip()
        if not component or component in excluded or component not in feature_names:
            continue
        horizon_value = to_float(row.get("horizon_days"))
        if horizon_value is None:
            continue
        horizon = int(horizon_value)
        if horizon not in horizons:
            continue
        if require_persistence and horizon in {60, 120} and str(row.get("persistent_60_120_flag") or "0").strip() not in {"1", "true", "True"}:
            continue
        direction = str(row.get("direction") or "").strip().lower()
        if direction not in {"positive", "inverse"}:
            continue
        cohort = str(row.get("calibration_cohort") or "").strip()
        if not cohort:
            continue
        q_values = [
            to_float(row.get("spearman_ic_excess_bh_q_value")),
            to_float(row.get("net_spearman_ic_excess_bh_q_value")),
            to_float(row.get("factor_neutral_spearman_ic_excess_bh_q_value")),
        ]
        q_clean = [value for value in q_values if value is not None]
        q_value = min(q_clean) if q_clean else 1.0
        ic = max(
            abs(to_float(row.get("spearman_ic_excess")) or 0.0),
            abs(to_float(row.get("net_spearman_ic_excess")) or 0.0),
            abs(to_float(row.get("factor_neutral_spearman_ic_excess")) or 0.0),
        )
        spread = max(
            abs(to_float(row.get("top_minus_bottom_median_excess")) or 0.0),
            abs(to_float(row.get("net_top_minus_bottom_median_excess")) or 0.0),
            abs(to_float(row.get("factor_neutral_top_minus_bottom_median_excess")) or 0.0),
        )
        coverage = (to_float(row.get("ticker_coverage_pct")) or 0.0) / 100.0
        key = (cohort, component, direction)
        item = grouped.setdefault(
            key,
            {
                "horizons": set(),
                "ic": 0.0,
                "spread": 0.0,
                "q_value": 1.0,
                "coverage": 0.0,
            },
        )
        item["horizons"].add(horizon)
        item["ic"] = max(float(item["ic"]), ic)
        item["spread"] = max(float(item["spread"]), spread)
        item["q_value"] = min(float(item["q_value"]), q_value)
        item["coverage"] = max(float(item["coverage"]), coverage)

    out: dict[str, list[ComponentCandidate]] = defaultdict(list)
    for (cohort, component, direction), item in grouped.items():
        persistent_bonus = 2.0 if {60, 120}.issubset(set(item["horizons"])) else 0.0
        quality = (
            persistent_bonus
            + 120.0 * float(item["ic"])
            + 80.0 * float(item["spread"])
            + 2.0 * float(item["coverage"])
            + max(0.0, 1.0 - float(item["q_value"]))
        )
        out[cohort].append(
            ComponentCandidate(
                component=component,
                direction=direction,
                quality=round(quality, 8),
                sleeve=component_sleeve(component),
                horizons=tuple(sorted(int(h) for h in item["horizons"])),
                ic=round(float(item["ic"]), 8),
                spread=round(float(item["spread"]), 8),
                q_value=round(float(item["q_value"]), 8),
            )
        )
    return {
        cohort: deduplicate_component_candidates(candidates, enabled=deduplicate_fingerprints)
        for cohort, candidates in out.items()
    }


def build_folds(rows: list[PanelRow], config: dict[str, Any]) -> list[Fold]:
    asof_dates = sorted({row.asof_date for row in rows})
    if not asof_dates:
        return []
    min_date = asof_dates[0]
    max_date = asof_dates[-1]
    train_months = cfg_int(config, "calibration.optuna_policy_optimizer.train_months", 12)
    validation_months = cfg_int(config, "calibration.optuna_policy_optimizer.validation_months", 3)
    step_months = cfg_int(config, "calibration.optuna_policy_optimizer.step_months", validation_months)
    embargo_days = cfg_int(config, "calibration.optuna_policy_optimizer.embargo_days", cfg_int(config, "calibration.embargo_days", 120))
    min_validation_calendar_days = cfg_int(
        config,
        "calibration.optuna_policy_optimizer.min_validation_calendar_days",
        45,
    )
    validation_start = add_months(min_date, train_months) + timedelta(days=embargo_days)
    folds: list[Fold] = []
    fold_no = 1
    while validation_start <= max_date:
        validation_end = min(add_months(validation_start, validation_months) - timedelta(days=1), max_date)
        validation_calendar_days = (validation_end - validation_start).days
        train_end = validation_start - timedelta(days=embargo_days + 1)
        train_start = add_months(train_end, -train_months) + timedelta(days=1)
        if train_start >= min_date and train_start <= train_end and validation_start <= validation_end:
            folds.append(
                Fold(
                    fold_id=f"wf_{fold_no:02d}",
                    train_start=train_start,
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                    validation_calendar_days=validation_calendar_days,
                    is_incomplete=validation_calendar_days < min_validation_calendar_days,
                )
            )
            fold_no += 1
        validation_start = add_months(validation_start, step_months)
    return folds


def candidate_component_score(row: PanelRow, component: str, direction: str) -> float:
    value = score_or(row, component, 50.0)
    return value if direction == "positive" else 100.0 - value


def score_candidate(row: PanelRow, candidate: TrialCandidate) -> float:
    if not candidate.components:
        return score_or(row, "ic_tilted_composite_score", score_or(row, "raw_composite_score", 50.0))
    value = sum(
        candidate_component_score(row, component, direction) * weight
        for component, direction, weight, _sleeve in candidate.components
    )
    return max(0.0, min(100.0, value))


def row_passes_gates(row: PanelRow, gates: dict[str, float]) -> bool:
    min_fields = {
        "raw_composite_min": "raw_composite_score",
        "ic_tilted_min": "ic_tilted_composite_score",
        "safe_core_min": "safe_core_score",
        "fundamental_quality_min": "fundamental_quality_score",
        "valuation_min": "valuation_score",
        "technical_entry_min": "technical_entry_score",
        "data_completeness_min": "data_completeness_score",
        "liquidity_min": "liquidity_score",
        "market_cap_min": "market_cap",
        "avg_dollar_volume_min": "avg_dollar_volume_60d",
    }
    max_fields = {
        "value_trap_max": "value_trap_score",
        "fda_event_risk_max": "fda_event_risk_score",
        "borrow_squeeze_risk_max": "borrow_squeeze_risk_score",
    }
    for gate, field in min_fields.items():
        threshold = gates.get(gate, 0.0)
        if threshold > 0.0 and row.features.get(field, -math.inf) < threshold:
            return False
    for gate, field in max_fields.items():
        threshold = gates.get(gate, 100.0)
        if threshold < 100.0 and row.features.get(field, math.inf) > threshold:
            return False
    if gates.get("require_tier1_safety_gate", 0.0) >= 1.0 and row.features.get("passed_tier1_safety_gate", 0.0) < 1.0:
        return False
    return True


def selected_ids_for_candidate(rows: list[PanelRow], candidate: TrialCandidate) -> set[int]:
    score_min = candidate.gates["candidate_score_min"]
    percentile_min = candidate.gates["candidate_percentile_min"]
    by_date: dict[date, list[tuple[PanelRow, float]]] = defaultdict(list)
    for row in rows:
        by_date[row.asof_date].append((row, score_candidate(row, candidate)))
    selected: set[int] = set()
    for items in by_date.values():
        ranked = sorted(items, key=lambda item: (-item[1], item[0].ticker))
        denom = max(1, len(ranked) - 1)
        for pos, (row, score) in enumerate(ranked):
            percentile = 100.0 if len(ranked) == 1 else 100.0 * (1.0 - pos / denom)
            if score >= score_min and percentile >= percentile_min and row_passes_gates(row, candidate.gates):
                selected.add(row.idx)
    return selected


def selected_baseline_ids(rows: list[PanelRow], mode: str) -> set[int]:
    selected: set[int] = set()
    for row in rows:
        if mode == "production":
            if row.features.get("final_investability_gate", 0.0) >= 1.0:
                selected.add(row.idx)
        elif mode == "topdecile":
            if row.cohort_rank_bucket == "cohort_top_decile" or row.features.get("cohort_percentile", -math.inf) >= 90.0:
                selected.add(row.idx)
        else:
            raise ValueError(f"Unknown baseline mode: {mode}")
    return selected


def rows_in_period(rows: list[PanelRow], start: date, end: date) -> list[PanelRow]:
    return [row for row in rows if start <= row.asof_date <= end]


def metric_for_selection(
    rows: list[PanelRow],
    selected: set[int],
    *,
    horizon: int,
    use_net: bool,
    full_ticker_count: int,
) -> dict[str, Any]:
    excess_field = f"{'net_' if use_net else ''}cohort_excess_return_{horizon}d"
    return_field = f"{'net_' if use_net else ''}forward_return_{horizon}d"
    selected_rows = [
        row
        for row in rows
        if row.idx in selected and excess_field in row.features and return_field in row.features
    ]
    if not selected_rows:
        return {
            "count": 0,
            "unique_tickers": 0,
            "ticker_coverage": 0.0,
            "excess_hit_count": 0,
            "mean_return": None,
            "median_return": None,
            "hit_rate": None,
            "mean_excess": None,
            "median_excess": None,
            "excess_hit_rate": None,
            "excess_hit_p_value": None,
            "loss_rate": None,
            "lcb_excess": None,
            "single_ticker_share": 0.0,
            "tickers": [],
        }
    returns = [row.features[return_field] for row in selected_rows]
    excess = [row.features[excess_field] for row in selected_rows]
    ticker_counts = Counter(row.ticker for row in selected_rows)
    excess_hit_count = sum(1 for value in excess if value > 0.0)
    return {
        "count": len(selected_rows),
        "unique_tickers": len(ticker_counts),
        "ticker_coverage": len(ticker_counts) / full_ticker_count if full_ticker_count else 0.0,
        "excess_hit_count": excess_hit_count,
        "mean_return": mean(returns),
        "median_return": median(returns),
        "hit_rate": sum(1 for value in returns if value > 0.0) / len(returns),
        "mean_excess": mean(excess),
        "median_excess": median(excess),
        "excess_hit_rate": excess_hit_count / len(excess),
        "excess_hit_p_value": None,
        "loss_rate": sum(1 for value in excess if value < 0.0) / len(excess),
        "lcb_excess": lcb(excess),
        "single_ticker_share": max(ticker_counts.values()) / len(selected_rows),
        "tickers": sorted(ticker_counts),
    }


def mean_clean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return mean(clean) if clean else None


def min_clean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return min(clean) if clean else None


def binomial_upper_tail_p_value(successes: int, total: int, p_null: float = 0.50) -> float | None:
    if total <= 0 or successes < 0 or successes > total:
        return None
    if successes <= 0:
        return 1.0
    if successes > total:
        return 0.0
    p_null = max(1e-12, min(1.0 - 1e-12, p_null))
    log_terms: list[float] = []
    for k in range(successes, total + 1):
        log_terms.append(
            math.lgamma(total + 1)
            - math.lgamma(k + 1)
            - math.lgamma(total - k + 1)
            + k * math.log(p_null)
            + (total - k) * math.log1p(-p_null)
        )
    max_log = max(log_terms)
    return min(1.0, math.exp(max_log) * sum(math.exp(value - max_log) for value in log_terms))


def hit_rate_guardrail_reason(metric: dict[str, Any], settings: dict[str, Any]) -> str:
    test_mode = str(settings["hit_rate_test"]).strip().lower()
    hit_count = int(metric.get("excess_hit_count") or 0)
    total = int(metric.get("count") or 0)
    hit_rate = metric.get("excess_hit_rate")
    if test_mode == "binomial":
        p_value = binomial_upper_tail_p_value(hit_count, total, settings["hit_rate_null"])
        metric["excess_hit_p_value"] = p_value
        if p_value is None:
            return "hit_rate_p_value_na"
        if p_value > settings["hit_rate_binomial_alpha"]:
            return f"hit_rate_p_{p_value:.3f}_above_alpha_{settings['hit_rate_binomial_alpha']:.2f}"
        return ""
    metric["excess_hit_p_value"] = None
    if hit_rate is None or hit_rate < settings["min_excess_hit_rate"]:
        hit_rate_value = 0.0 if hit_rate is None else hit_rate
        return f"hit_rate_{hit_rate_value:.2f}_below_{settings['min_excess_hit_rate']:.2f}"
    return ""


def guardrail_reasons(
    *,
    metric: dict[str, Any],
    train_metric: dict[str, Any],
    settings: dict[str, Any],
    horizon: int,
) -> list[str]:
    reasons: list[str] = []
    if train_metric["count"] < settings["min_train_selected"]:
        reasons.append(f"train_selected_{train_metric['count']}_below_{settings['min_train_selected']}")
    if train_metric["unique_tickers"] < settings["min_train_unique_tickers"]:
        reasons.append(f"train_unique_{train_metric['unique_tickers']}_below_{settings['min_train_unique_tickers']}")
    if metric["count"] < settings["min_validation_selected"]:
        reasons.append(f"validation_selected_{metric['count']}_below_{settings['min_validation_selected']}")
    if metric["unique_tickers"] < settings["min_validation_unique_tickers"]:
        reasons.append(f"validation_unique_{metric['unique_tickers']}_below_{settings['min_validation_unique_tickers']}")
    if metric["ticker_coverage"] < settings["min_selected_ticker_coverage"]:
        reasons.append(f"ticker_coverage_{metric['ticker_coverage']:.2f}_below_{settings['min_selected_ticker_coverage']:.2f}")
    hit_reason = hit_rate_guardrail_reason(metric, settings)
    if hit_reason:
        reasons.append(hit_reason)
    if metric["loss_rate"] is None or metric["loss_rate"] > settings["max_loss_rate"]:
        loss_rate = 1.0 if metric["loss_rate"] is None else metric["loss_rate"]
        reasons.append(f"loss_rate_{loss_rate:.2f}_above_{settings['max_loss_rate']:.2f}")
    if metric["single_ticker_share"] > settings["max_single_ticker_share"]:
        reasons.append(
            f"single_ticker_share_{metric['single_ticker_share']:.2f}_above_{settings['max_single_ticker_share']:.2f}"
        )
    if horizon in settings["require_positive_lcb_horizons"]:
        lcb_value = metric["lcb_excess"]
        if lcb_value is None or lcb_value < settings["min_lcb_excess"]:
            lcb_text = "na" if lcb_value is None else f"{lcb_value:.4f}"
            reasons.append(f"lcb_{lcb_text}_below_{settings['min_lcb_excess']:.4f}")
    return reasons


def max_single_ticker_share_limit(settings: dict[str, Any], full_ticker_count: int) -> float:
    base_limit = settings["max_single_ticker_share"]
    if full_ticker_count <= settings["small_cohort_ticker_threshold"]:
        return max(base_limit, settings["max_single_ticker_share_small_cohort"])
    return base_limit


def evaluate_candidate(
    *,
    candidate: TrialCandidate,
    cohort_rows: list[PanelRow],
    folds: list[Fold],
    horizons: list[int],
    settings: dict[str, Any],
) -> dict[str, Any]:
    selected = selected_ids_for_candidate(cohort_rows, candidate)
    topdecile_selected = selected_baseline_ids(cohort_rows, "topdecile")
    production_selected = selected_baseline_ids(cohort_rows, "production")
    full_ticker_count = len({row.ticker for row in cohort_rows})
    effective_settings = dict(settings)
    effective_settings["max_single_ticker_share"] = max_single_ticker_share_limit(settings, full_ticker_count)
    fold_passes: dict[str, bool] = {}
    fold_reasons: dict[str, list[str]] = defaultdict(list)
    metrics_by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    topdecile_by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    production_by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    all_validation_tickers: set[str] = set()
    validation_count = 0
    fold_diagnostics: list[dict[str, Any]] = []

    for fold in folds:
        fold_pass = True
        count_fold = not (fold.is_incomplete and settings["exclude_incomplete_validation_folds"])
        train_rows = rows_in_period(cohort_rows, fold.train_start, fold.train_end)
        validation_rows = rows_in_period(cohort_rows, fold.validation_start, fold.validation_end)
        if not train_rows or not validation_rows:
            continue
        for horizon in horizons:
            train_metric = metric_for_selection(
                train_rows,
                selected,
                horizon=horizon,
                use_net=settings["use_net_excess"],
                full_ticker_count=full_ticker_count,
            )
            metric = metric_for_selection(
                validation_rows,
                selected,
                horizon=horizon,
                use_net=settings["use_net_excess"],
                full_ticker_count=full_ticker_count,
            )
            topdecile_metric = metric_for_selection(
                validation_rows,
                topdecile_selected,
                horizon=horizon,
                use_net=settings["use_net_excess"],
                full_ticker_count=full_ticker_count,
            )
            production_metric = metric_for_selection(
                validation_rows,
                production_selected,
                horizon=horizon,
                use_net=settings["use_net_excess"],
                full_ticker_count=full_ticker_count,
            )
            if count_fold:
                reasons = guardrail_reasons(metric=metric, train_metric=train_metric, settings=effective_settings, horizon=horizon)
            else:
                reasons = [
                    f"incomplete_validation_fold_{fold.validation_calendar_days}_days_below_{settings['min_validation_calendar_days']}"
                ]
            if reasons:
                fold_pass = False
                fold_reasons[fold.fold_id].extend([f"{horizon}d_{reason}" for reason in reasons])
            fold_diagnostics.append(
                {
                    "fold_id": fold.fold_id,
                    "is_incomplete_fold": int(fold.is_incomplete),
                    "counted_in_pass_rate": int(count_fold),
                    "horizon_days": horizon,
                    "validation_start": fold.validation_start.isoformat(),
                    "validation_end": fold.validation_end.isoformat(),
                    "selected_count": metric["count"],
                    "unique_tickers": metric["unique_tickers"],
                    "ticker_coverage": metric["ticker_coverage"],
                    "excess_hit_count": metric["excess_hit_count"],
                    "excess_hit_rate": metric["excess_hit_rate"],
                    "excess_hit_p_value": metric.get("excess_hit_p_value"),
                    "loss_rate": metric["loss_rate"],
                    "lcb_excess": metric["lcb_excess"],
                    "single_ticker_share": metric["single_ticker_share"],
                    "fold_horizon_status": "pass" if not reasons else ("incomplete_excluded" if not count_fold else "fail"),
                    "guardrail_reason": ";".join(dict.fromkeys(reasons)),
                    "selected_tickers": ",".join(metric["tickers"]),
                }
            )
            if not count_fold:
                continue
            metric["fold_id"] = fold.fold_id
            topdecile_metric["fold_id"] = fold.fold_id
            production_metric["fold_id"] = fold.fold_id
            metrics_by_horizon[horizon].append(metric)
            topdecile_by_horizon[horizon].append(topdecile_metric)
            production_by_horizon[horizon].append(production_metric)
            validation_count += metric["count"]
            all_validation_tickers.update(metric["tickers"])
        if count_fold:
            fold_passes[fold.fold_id] = fold_pass

    pass_fold_count = sum(1 for value in fold_passes.values() if value)
    fold_count = len(fold_passes)
    pass_fold_rate = pass_fold_count / fold_count if fold_count else 0.0
    passing_fold_ids = {fold_id for fold_id, passed in fold_passes.items() if passed}
    all_metrics = [metric for horizon in horizons for metric in metrics_by_horizon[horizon]]
    mean_excess_hit_rate = mean_clean([metric["excess_hit_rate"] for metric in all_metrics])
    mean_loss_rate = mean_clean([metric["loss_rate"] for metric in all_metrics])
    max_single_ticker_share = max((metric["single_ticker_share"] for metric in all_metrics), default=0.0)

    result: dict[str, Any] = {
        "calibration_cohort": candidate.cohort,
        "candidate_id": candidate.candidate_id,
        "fold_count": fold_count,
        "pass_fold_count": pass_fold_count,
        "pass_fold_rate": pass_fold_rate,
        "validation_count": validation_count,
        "validation_unique_tickers": len(all_validation_tickers),
        "validation_ticker_coverage": len(all_validation_tickers) / full_ticker_count if full_ticker_count else 0.0,
        "mean_excess_hit_rate": mean_excess_hit_rate,
        "mean_loss_rate": mean_loss_rate,
        "max_single_ticker_share": max_single_ticker_share,
        "max_single_ticker_share_limit": effective_settings["max_single_ticker_share"],
        "min_lcb_scope": settings["min_lcb_scope"],
        "candidate_reason": compact_reasons([reason for reasons in fold_reasons.values() for reason in reasons]),
        "fold_diagnostics": fold_diagnostics,
    }
    for horizon in horizons:
        lcb_metric_pool = metrics_by_horizon[horizon]
        if settings["min_lcb_scope"] == "passing_folds_only":
            lcb_metric_pool = [metric for metric in lcb_metric_pool if metric.get("fold_id") in passing_fold_ids]
        candidate_lcbs = [metric["lcb_excess"] for metric in lcb_metric_pool]
        candidate_medians = [metric["median_excess"] for metric in metrics_by_horizon[horizon]]
        topdecile_lcbs = [metric["lcb_excess"] for metric in topdecile_by_horizon[horizon]]
        production_lcbs = [metric["lcb_excess"] for metric in production_by_horizon[horizon]]
        mean_lcb_value = mean_clean(candidate_lcbs)
        mean_topdecile_lcb = mean_clean(topdecile_lcbs)
        mean_production_lcb = mean_clean(production_lcbs)
        result[f"mean_lcb_excess_{horizon}d"] = mean_lcb_value
        result[f"min_lcb_excess_{horizon}d"] = min_clean(candidate_lcbs)
        result[f"mean_median_excess_{horizon}d"] = mean_clean(candidate_medians)
        result[f"delta_lcb_vs_topdecile_{horizon}d"] = (
            mean_lcb_value - mean_topdecile_lcb
            if mean_lcb_value is not None and mean_topdecile_lcb is not None
            else None
        )
        result[f"delta_lcb_vs_production_{horizon}d"] = (
            mean_lcb_value - mean_production_lcb
            if mean_lcb_value is not None and mean_production_lcb is not None
            else None
        )

    candidate_reasons: list[str] = []
    if pass_fold_count < settings["min_pass_folds"]:
        candidate_reasons.append(f"pass_folds_{pass_fold_count}_below_{settings['min_pass_folds']}")
    if pass_fold_rate < settings["min_pass_fold_rate"]:
        candidate_reasons.append(f"pass_fold_rate_{pass_fold_rate:.2f}_below_{settings['min_pass_fold_rate']:.2f}")
    if len(all_validation_tickers) < settings["min_summary_unique_tickers"]:
        candidate_reasons.append(
            f"summary_unique_{len(all_validation_tickers)}_below_{settings['min_summary_unique_tickers']}"
        )
    for horizon in settings["require_positive_lcb_horizons"]:
        min_lcb_value = result.get(f"min_lcb_excess_{horizon}d")
        if min_lcb_value is None or min_lcb_value < settings["min_lcb_excess"]:
            lcb_text = "na" if min_lcb_value is None else f"{min_lcb_value:.4f}"
            candidate_reasons.append(f"{horizon}d_min_lcb_{lcb_text}_below_{settings['min_lcb_excess']:.4f}")
    if mean_loss_rate is None or mean_loss_rate > effective_settings["max_loss_rate"]:
        loss_text = "na" if mean_loss_rate is None else f"{mean_loss_rate:.2f}"
        candidate_reasons.append(f"mean_loss_rate_{loss_text}_above_{effective_settings['max_loss_rate']:.2f}")
    if max_single_ticker_share > effective_settings["max_single_ticker_share"]:
        candidate_reasons.append(
            f"max_single_ticker_share_{max_single_ticker_share:.2f}_above_{effective_settings['max_single_ticker_share']:.2f}"
        )

    sleeve_weights = sleeve_weight_map(candidate)
    max_sleeve_weight = max(sleeve_weights.values(), default=0.0)
    max_component_weight = max((weight for _, _, weight, _ in candidate.components), default=0.0)
    if max_sleeve_weight > settings["max_sleeve_weight"] + 1e-9:
        candidate_reasons.append(f"sleeve_weight_{max_sleeve_weight:.2f}_above_{settings['max_sleeve_weight']:.2f}")
    if max_component_weight > settings["max_component_weight"] + 1e-9:
        candidate_reasons.append(
            f"component_weight_{max_component_weight:.2f}_above_{settings['max_component_weight']:.2f}"
        )

    if candidate_reasons:
        result["candidate_status"] = "research_only"
        result["candidate_reason"] = compact_reasons(candidate_reasons + [result["candidate_reason"]])
    else:
        result["candidate_status"] = "promotion_review_candidate"
        result["candidate_reason"] = "passes_optuna_shadow_guardrails_not_auto_promoted"
    result["objective_value"] = objective_value(result, settings)
    return result


def sleeve_weight_map(candidate: TrialCandidate) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for _component, _direction, weight, sleeve in candidate.components:
        out[sleeve] += weight
    return {key: round(value, 6) for key, value in sorted(out.items())}


def objective_value(result: dict[str, Any], settings: dict[str, Any]) -> float:
    lcb_values = finite_float_values([
        result.get(f"mean_lcb_excess_{horizon}d")
        for horizon in settings["horizons"]
        if result.get(f"mean_lcb_excess_{horizon}d") is not None
    ])
    min_lcb_values = finite_float_values([
        result.get(f"min_lcb_excess_{horizon}d")
        for horizon in settings["horizons"]
        if result.get(f"min_lcb_excess_{horizon}d") is not None
    ])
    median_values = finite_float_values([
        result.get(f"mean_median_excess_{horizon}d")
        for horizon in settings["horizons"]
        if result.get(f"mean_median_excess_{horizon}d") is not None
    ])
    delta_values = finite_float_values([
        result.get(f"delta_lcb_vs_topdecile_{horizon}d")
        for horizon in settings["horizons"]
        if result.get(f"delta_lcb_vs_topdecile_{horizon}d") is not None
    ])
    avg_lcb = mean(lcb_values) if lcb_values else -0.25
    worst_lcb = min(min_lcb_values) if min_lcb_values else -0.25
    avg_median = mean(median_values) if median_values else -0.25
    avg_delta = mean(delta_values) if delta_values else 0.0
    hit_rate = result["mean_excess_hit_rate"] if result["mean_excess_hit_rate"] is not None else 0.0
    loss_rate = result["mean_loss_rate"] if result["mean_loss_rate"] is not None else 1.0
    pass_rate = result["pass_fold_rate"]
    coverage = result["validation_ticker_coverage"]
    single_ticker_limit = result.get("max_single_ticker_share_limit", settings["max_single_ticker_share"])
    unique_deficit = max(0, settings["min_summary_unique_tickers"] - result["validation_unique_tickers"])
    fold_deficit = max(0, settings["min_pass_folds"] - result["pass_fold_count"])
    pass_rate_deficit = max(0.0, settings["min_pass_fold_rate"] - pass_rate)
    coverage_deficit = max(0.0, settings["min_selected_ticker_coverage"] - coverage)
    loss_excess = max(0.0, loss_rate - settings["max_loss_rate"])
    concentration_penalty = max(0.0, result["max_single_ticker_share"] - single_ticker_limit)
    guardrail_penalty = 0.0 if result["candidate_status"] == "promotion_review_candidate" else 100.0
    value = (
        130.0 * avg_lcb
        + 70.0 * worst_lcb
        + 55.0 * avg_median
        + 45.0 * avg_delta
        + 15.0 * hit_rate
        + 35.0 * pass_rate
        + 16.0 * coverage
        - 24.0 * loss_rate
        - 30.0 * loss_excess
        - 75.0 * concentration_penalty
        - 18.0 * unique_deficit
        - 24.0 * fold_deficit
        - 75.0 * pass_rate_deficit
        - 35.0 * coverage_deficit
        - guardrail_penalty
    )
    return round(value, 8)


def infeasible_prune_reason(result: dict[str, Any], settings: dict[str, Any]) -> str:
    if settings["prune_zero_validation_selected"] and int(result.get("validation_count") or 0) == 0:
        return "zero_validation_selected"
    min_unique = int(settings["prune_min_validation_unique_tickers"])
    if min_unique > 0 and int(result.get("validation_unique_tickers") or 0) < min_unique:
        return f"validation_unique_tickers_below_{min_unique}"
    return ""


def sample_gate(trial: optuna.Trial, name: str, values: list[float]) -> float:
    return float(trial.suggest_categorical(name, sorted(set(values))))


def capped_allocation(raw_weights: dict[str, float], caps: dict[str, float], *, total: float = 1.0) -> dict[str, float] | None:
    active = {key for key, value in raw_weights.items() if value > 0.0}
    if not active:
        return None
    if sum(caps.get(key, 0.0) for key in active) + 1e-12 < total:
        return None
    allocated: dict[str, float] = {}
    remaining_total = total
    while active:
        raw_total = sum(raw_weights[key] for key in active)
        if raw_total <= 0.0:
            share = remaining_total / len(active)
            tentative = {key: share for key in active}
        else:
            tentative = {key: remaining_total * raw_weights[key] / raw_total for key in active}
        over_cap = [key for key, value in tentative.items() if value > caps.get(key, 0.0) + 1e-12]
        if not over_cap:
            allocated.update(tentative)
            break
        for key in over_cap:
            cap = caps.get(key, 0.0)
            allocated[key] = cap
            remaining_total -= cap
            active.remove(key)
        if remaining_total < -1e-9:
            return None
        if remaining_total <= 1e-12:
            for key in active:
                allocated[key] = 0.0
            break
        if sum(caps.get(key, 0.0) for key in active) + 1e-12 < remaining_total:
            return None
    scale = sum(allocated.values())
    if scale <= 0.0:
        return None
    return {key: value * total / scale for key, value in allocated.items()}


def project_component_weights(
    items: list[ComponentCandidate],
    raw_weights: list[float],
    *,
    max_component_weight: float,
    max_sleeve_weight: float,
) -> list[float] | None:
    if not items or len(items) != len(raw_weights):
        return None
    sleeve_raw: dict[str, float] = defaultdict(float)
    for item, raw_weight in zip(items, raw_weights):
        sleeve_raw[item.sleeve] += max(0.0, raw_weight)
    sleeve_caps = {sleeve: max_sleeve_weight for sleeve in sleeve_raw}
    sleeve_weights = capped_allocation(sleeve_raw, sleeve_caps, total=1.0)
    if sleeve_weights is None:
        return None
    out_by_component: dict[int, float] = {}
    by_sleeve: dict[str, list[tuple[int, ComponentCandidate, float]]] = defaultdict(list)
    for idx, (item, raw_weight) in enumerate(zip(items, raw_weights)):
        by_sleeve[item.sleeve].append((idx, item, max(0.0, raw_weight)))
    for sleeve, sleeve_items in by_sleeve.items():
        sleeve_total = sleeve_weights.get(sleeve, 0.0)
        if sleeve_total <= 0.0:
            for idx, _item, _raw in sleeve_items:
                out_by_component[idx] = 0.0
            continue
        component_raw = {str(idx): raw_weight for idx, _item, raw_weight in sleeve_items}
        component_caps = {str(idx): min(max_component_weight, sleeve_total) for idx, _item, _raw in sleeve_items}
        allocated = capped_allocation(component_raw, component_caps, total=sleeve_total)
        if allocated is None:
            return None
        for idx, _item, _raw in sleeve_items:
            out_by_component[idx] = allocated.get(str(idx), 0.0)
    weights = [out_by_component.get(idx, 0.0) for idx in range(len(items))]
    total = sum(weights)
    if total <= 0.0:
        return None
    weights = [weight / total for weight in weights]
    if max(weights, default=0.0) > max_component_weight + 1e-8:
        return None
    sleeve_totals: dict[str, float] = defaultdict(float)
    for item, weight in zip(items, weights):
        sleeve_totals[item.sleeve] += weight
    if max(sleeve_totals.values(), default=0.0) > max_sleeve_weight + 1e-8:
        return None
    return weights


def build_settings(config: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    min_lcb_scope = str(
        cfg_get(config, "calibration.optuna_policy_optimizer.min_lcb_scope", "passing_folds_only")
    ).strip().lower()
    if min_lcb_scope not in {"passing_folds_only", "all_folds"}:
        raise ValueError("calibration.optuna_policy_optimizer.min_lcb_scope must be passing_folds_only or all_folds")
    hit_rate_test = str(
        cfg_get(config, "calibration.optuna_policy_optimizer.hit_rate_test", "binomial")
    ).strip().lower()
    if hit_rate_test not in {"binomial", "threshold"}:
        raise ValueError("calibration.optuna_policy_optimizer.hit_rate_test must be binomial or threshold")
    return {
        "horizons": horizons,
        "use_net_excess": cfg_bool(config, "calibration.optuna_policy_optimizer.use_net_excess", True),
        "min_lcb_scope": min_lcb_scope,
        "exclude_incomplete_validation_folds": cfg_bool(
            config,
            "calibration.optuna_policy_optimizer.exclude_incomplete_validation_folds",
            True,
        ),
        "min_validation_calendar_days": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.min_validation_calendar_days",
            45,
        ),
        "hit_rate_test": hit_rate_test,
        "hit_rate_null": cfg_float(config, "calibration.optuna_policy_optimizer.hit_rate_null", 0.50),
        "hit_rate_binomial_alpha": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.hit_rate_binomial_alpha",
            0.10,
        ),
        "min_train_selected": cfg_int(config, "calibration.optuna_policy_optimizer.min_train_selected", 20),
        "min_train_unique_tickers": cfg_int(config, "calibration.optuna_policy_optimizer.min_train_unique_tickers", 3),
        "min_validation_selected": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.min_validation_selected",
            cfg_int(config, "calibration.template_walk_forward.min_validation_selected", 10),
        ),
        "min_validation_unique_tickers": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.min_validation_unique_tickers",
            cfg_int(config, "calibration.template_walk_forward.min_validation_unique_tickers", 3),
        ),
        "min_selected_ticker_coverage": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.min_selected_ticker_coverage",
            cfg_float(config, "calibration.template_walk_forward.min_selected_ticker_coverage", 0.10),
        ),
        "min_excess_hit_rate": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.min_excess_hit_rate",
            cfg_float(config, "calibration.template_walk_forward.min_excess_hit_rate", 0.52),
        ),
        "max_loss_rate": cfg_float(config, "calibration.optuna_policy_optimizer.max_loss_rate", 0.45),
        "max_single_ticker_share": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.max_single_ticker_share",
            cfg_float(config, "calibration.component_promotion_review.max_single_ticker_share", 0.35),
        ),
        "max_single_ticker_share_small_cohort": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.max_single_ticker_share_small_cohort",
            0.50,
        ),
        "small_cohort_ticker_threshold": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.small_cohort_ticker_threshold",
            16,
        ),
        "min_lcb_excess": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.min_lcb_excess",
            cfg_float(config, "calibration.min_validation_lcb_excess", 0.0),
        ),
        "require_positive_lcb_horizons": set(
            parse_int_list(
                cfg_get(
                    config,
                    "calibration.optuna_policy_optimizer.require_positive_lcb_horizons",
                    cfg_get(config, "calibration.require_positive_lcb_horizons", "60,120"),
                )
            )
        ),
        "min_pass_folds": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.min_pass_folds",
            cfg_int(config, "calibration.template_walk_forward.min_pass_folds", 2),
        ),
        "min_pass_fold_rate": cfg_float(
            config,
            "calibration.optuna_policy_optimizer.min_pass_fold_rate",
            cfg_float(config, "calibration.template_walk_forward.min_pass_fold_rate", 0.60),
        ),
        "min_summary_unique_tickers": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.min_summary_unique_tickers",
            cfg_int(config, "calibration.template_walk_forward.min_summary_unique_tickers", 3),
        ),
        "max_component_weight": cfg_float(config, "calibration.optuna_policy_optimizer.max_component_weight", 0.65),
        "max_sleeve_weight": cfg_float(config, "calibration.optuna_policy_optimizer.max_sleeve_weight", 0.65),
        "prune_zero_validation_selected": cfg_bool(
            config,
            "calibration.optuna_policy_optimizer.prune_zero_validation_selected",
            True,
        ),
        "prune_min_validation_unique_tickers": cfg_int(
            config,
            "calibration.optuna_policy_optimizer.prune_min_validation_unique_tickers",
            2,
        ),
    }


def sample_candidate(
    *,
    trial: optuna.Trial,
    cohort: str,
    candidates: list[ComponentCandidate],
    config: dict[str, Any],
) -> TrialCandidate:
    pool_size = cfg_int(config, "calibration.optuna_policy_optimizer.component_pool_size", 12)
    pool = candidates[: max(1, min(pool_size, len(candidates)))]
    min_components = min(
        cfg_int(config, "calibration.optuna_policy_optimizer.min_components_per_candidate", 2),
        len(pool),
    )
    max_components = min(
        cfg_int(config, "calibration.optuna_policy_optimizer.max_components_per_candidate", 5),
        len(pool),
    )
    if max_components < 1:
        raise ValueError(f"No eligible components for cohort {cohort}")
    min_components = max(1, min(min_components, max_components))
    chosen_indexes = [
        idx
        for idx in range(len(pool))
        if trial.suggest_categorical(f"include_component_{idx}", [False, True])
    ]
    if len(chosen_indexes) > max_components:
        chosen_indexes = sorted(chosen_indexes, key=lambda idx: pool[idx].quality, reverse=True)[:max_components]
    for idx, _candidate in enumerate(pool):
        if len(chosen_indexes) >= min_components:
            break
        if idx not in chosen_indexes:
            chosen_indexes.append(idx)
    chosen_items = [pool[idx] for idx in chosen_indexes]
    raw_weights = [trial.suggest_float(f"component_weight_{idx}", 0.05, 1.0) for idx in chosen_indexes]
    max_component_weight = cfg_float(config, "calibration.optuna_policy_optimizer.max_component_weight", 0.65)
    max_sleeve_weight = cfg_float(config, "calibration.optuna_policy_optimizer.max_sleeve_weight", 0.65)
    projected_weights = project_component_weights(
        chosen_items,
        raw_weights,
        max_component_weight=max_component_weight,
        max_sleeve_weight=max_sleeve_weight,
    )
    if projected_weights is None:
        trial.set_user_attr("prune_reason", "weight_projection_infeasible")
        raise optuna.TrialPruned("weight_projection_infeasible")
    components: list[tuple[str, str, float, str]] = []
    for item, projected_weight in zip(chosen_items, projected_weights):
        components.append(
            (
                item.component,
                item.direction,
                round(projected_weight, 8),
                item.sleeve,
            )
        )
    components.sort(key=lambda item: (-item[2], item[0], item[1]))
    gates = {
        "candidate_score_min": sample_gate(
            trial,
            "candidate_score_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.candidate_score_min", "50,55,60,65,70"),
                [50.0, 55.0, 60.0, 65.0, 70.0],
            ),
        ),
        "candidate_percentile_min": sample_gate(
            trial,
            "candidate_percentile_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.candidate_percentile_min", "70,80,85,90"),
                [70.0, 80.0, 85.0, 90.0],
            ),
        ),
        "raw_composite_min": sample_gate(
            trial,
            "raw_composite_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.raw_composite_min", "0,50,55,60"),
                [0.0, 50.0, 55.0, 60.0],
            ),
        ),
        "ic_tilted_min": sample_gate(
            trial,
            "ic_tilted_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.ic_tilted_min", "0,50,55,60"),
                [0.0, 50.0, 55.0, 60.0],
            ),
        ),
        "safe_core_min": sample_gate(
            trial,
            "safe_core_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.safe_core_min", "0,55,60,65"),
                [0.0, 55.0, 60.0, 65.0],
            ),
        ),
        "fundamental_quality_min": sample_gate(
            trial,
            "fundamental_quality_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.fundamental_quality_min", "0,55,60,65"),
                [0.0, 55.0, 60.0, 65.0],
            ),
        ),
        "valuation_min": sample_gate(
            trial,
            "valuation_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.valuation_min", "0,45,50,55"),
                [0.0, 45.0, 50.0, 55.0],
            ),
        ),
        "technical_entry_min": sample_gate(
            trial,
            "technical_entry_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.technical_entry_min", "0,45,55"),
                [0.0, 45.0, 55.0],
            ),
        ),
        "data_completeness_min": sample_gate(
            trial,
            "data_completeness_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.data_completeness_min", "80,85,90"),
                [80.0, 85.0, 90.0],
            ),
        ),
        "liquidity_min": sample_gate(
            trial,
            "liquidity_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.liquidity_min", "0,40,50"),
                [0.0, 40.0, 50.0],
            ),
        ),
        "value_trap_max": sample_gate(
            trial,
            "value_trap_max",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.value_trap_max", "25,30,35,40,50"),
                [25.0, 30.0, 35.0, 40.0, 50.0],
            ),
        ),
        "fda_event_risk_max": sample_gate(
            trial,
            "fda_event_risk_max",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.fda_event_risk_max", "35,40,50,100"),
                [35.0, 40.0, 50.0, 100.0],
            ),
        ),
        "borrow_squeeze_risk_max": sample_gate(
            trial,
            "borrow_squeeze_risk_max",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.borrow_squeeze_risk_max", "60,70,80,100"),
                [60.0, 70.0, 80.0, 100.0],
            ),
        ),
        "market_cap_min": sample_gate(
            trial,
            "market_cap_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.market_cap_min", "0,250000000,500000000"),
                [0.0, 250000000.0, 500000000.0],
            ),
        ),
        "avg_dollar_volume_min": sample_gate(
            trial,
            "avg_dollar_volume_min",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.avg_dollar_volume_min", "0,1000000,2000000"),
                [0.0, 1000000.0, 2000000.0],
            ),
        ),
        "require_tier1_safety_gate": sample_gate(
            trial,
            "require_tier1_safety_gate",
            parse_float_list(
                cfg_get(config, "calibration.optuna_policy_optimizer.require_tier1_safety_gate", "0,1"),
                [0.0, 1.0],
            ),
        ),
    }
    payload = {
        "cohort": cohort,
        "components": components,
        "gates": gates,
    }
    digest = hashlib.sha1(json_dumps(payload).encode("utf-8")).hexdigest()[:12]
    return TrialCandidate(cohort=cohort, components=tuple(components), gates=gates, candidate_id=f"optuna_{digest}")


def trial_row(trial: optuna.trial.FrozenTrial, result: dict[str, Any], candidate: TrialCandidate) -> dict[str, Any]:
    row: dict[str, Any] = {field: "" for field in TRIAL_FIELDS}
    row.update(
        {
            "calibration_cohort": result["calibration_cohort"],
            "trial_number": trial.number,
            "trial_state": trial.state.name,
            "prune_reason": trial.user_attrs.get("prune_reason", ""),
            "candidate_id": result["candidate_id"],
            "objective_value": fmt(result["objective_value"]),
            "candidate_status": result["candidate_status"],
            "candidate_reason": result["candidate_reason"],
            "fold_count": result["fold_count"],
            "pass_fold_count": result["pass_fold_count"],
            "pass_fold_rate": fmt(result["pass_fold_rate"]),
            "validation_count": result["validation_count"],
            "validation_unique_tickers": result["validation_unique_tickers"],
            "validation_ticker_coverage": fmt(result["validation_ticker_coverage"]),
            "mean_excess_hit_rate": fmt(result["mean_excess_hit_rate"]),
            "mean_loss_rate": fmt(result["mean_loss_rate"]),
            "max_single_ticker_share": fmt(result["max_single_ticker_share"]),
            "max_single_ticker_share_limit": fmt(result.get("max_single_ticker_share_limit")),
            "min_lcb_scope": result.get("min_lcb_scope", ""),
            "component_count": len(candidate.components),
            "component_spec_json": json_dumps(
                [
                    {"component": component, "direction": direction, "weight": weight, "sleeve": sleeve}
                    for component, direction, weight, sleeve in candidate.components
                ]
            ),
            "sleeve_weight_json": json_dumps(sleeve_weight_map(candidate)),
            "gate_spec_json": json_dumps(candidate.gates),
            "params_json": json_dumps(trial.params),
        }
    )
    for horizon in [60, 120]:
        row[f"mean_lcb_excess_{horizon}d"] = fmt(result.get(f"mean_lcb_excess_{horizon}d"))
        row[f"min_lcb_excess_{horizon}d"] = fmt(result.get(f"min_lcb_excess_{horizon}d"))
        row[f"delta_lcb_vs_topdecile_{horizon}d"] = fmt(result.get(f"delta_lcb_vs_topdecile_{horizon}d"))
        row[f"delta_lcb_vs_production_{horizon}d"] = fmt(result.get(f"delta_lcb_vs_production_{horizon}d"))
        row[f"mean_median_excess_{horizon}d"] = fmt(result.get(f"mean_median_excess_{horizon}d"))
    return row


def summarize_cohort(
    cohort: str,
    trial_rows_for_cohort: list[dict[str, Any]],
    candidates: list[ComponentCandidate],
) -> dict[str, Any]:
    completed = [row for row in trial_rows_for_cohort if row.get("trial_state") == "COMPLETE"]
    pruned = [row for row in trial_rows_for_cohort if row.get("trial_state") == "PRUNED"]
    review_candidates = [row for row in completed if row.get("candidate_status") == "promotion_review_candidate"]
    best_pool = review_candidates or completed or trial_rows_for_cohort
    best = max(best_pool, key=lambda row: to_float(row.get("objective_value")) or -math.inf)
    return {
        "calibration_cohort": cohort,
        "candidate_count": len(trial_rows_for_cohort),
        "completed_candidate_count": len(completed),
        "pruned_candidate_count": len(pruned),
        "best_candidate_id": best["candidate_id"],
        "best_objective_value": best["objective_value"],
        "best_candidate_status": best["candidate_status"],
        "best_candidate_reason": best["candidate_reason"],
        "best_pass_fold_rate": best["pass_fold_rate"],
        "best_validation_unique_tickers": best["validation_unique_tickers"],
        "best_min_lcb_excess_60d": best["min_lcb_excess_60d"],
        "best_min_lcb_excess_120d": best["min_lcb_excess_120d"],
        "best_mean_loss_rate": best["mean_loss_rate"],
        "best_max_single_ticker_share": best["max_single_ticker_share"],
        "best_max_single_ticker_share_limit": best["max_single_ticker_share_limit"],
        "best_min_lcb_scope": best["min_lcb_scope"],
        "eligible_component_count": len(candidates),
        "eligible_components_json": json_dumps(
            [
                {
                    "component": item.component,
                    "direction": item.direction,
                    "sleeve": item.sleeve,
                    "quality": item.quality,
                    "horizons": item.horizons,
                }
                for item in candidates
            ]
        ),
    }


def best_recommendable_row(trial_rows_for_cohort: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in trial_rows_for_cohort if row.get("trial_state") == "COMPLETE"]
    review_candidates = [row for row in completed if row.get("candidate_status") == "promotion_review_candidate"]
    best_pool = review_candidates or completed or trial_rows_for_cohort
    return max(best_pool, key=lambda row: to_float(row.get("objective_value")) or -math.inf)


def recommendation_row(best: dict[str, Any]) -> dict[str, Any]:
    status = "shadow_promotion_review" if best["candidate_status"] == "promotion_review_candidate" else "research_only"
    return {
        "calibration_cohort": best["calibration_cohort"],
        "recommended_candidate_id": best["candidate_id"],
        "promotion_status": status,
        "objective_value": best["objective_value"],
        "pass_fold_count": best["pass_fold_count"],
        "pass_fold_rate": best["pass_fold_rate"],
        "validation_count": best["validation_count"],
        "validation_unique_tickers": best["validation_unique_tickers"],
        "validation_ticker_coverage": best["validation_ticker_coverage"],
        "mean_excess_hit_rate": best["mean_excess_hit_rate"],
        "mean_loss_rate": best["mean_loss_rate"],
        "max_single_ticker_share": best["max_single_ticker_share"],
        "max_single_ticker_share_limit": best["max_single_ticker_share_limit"],
        "min_lcb_scope": best["min_lcb_scope"],
        "mean_lcb_excess_60d": best["mean_lcb_excess_60d"],
        "min_lcb_excess_60d": best["min_lcb_excess_60d"],
        "delta_lcb_vs_topdecile_60d": best["delta_lcb_vs_topdecile_60d"],
        "mean_lcb_excess_120d": best["mean_lcb_excess_120d"],
        "min_lcb_excess_120d": best["min_lcb_excess_120d"],
        "delta_lcb_vs_topdecile_120d": best["delta_lcb_vs_topdecile_120d"],
        "promotion_reason": best["candidate_reason"],
        "component_spec_json": best["component_spec_json"],
        "sleeve_weight_json": best["sleeve_weight_json"],
        "gate_spec_json": best["gate_spec_json"],
    }


def write_config_fragment(path: Path, recommendations: list[dict[str, Any]]) -> None:
    payload = {
        "calibration": {
            "optuna_shadow_policy_candidates": {
                "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "promotion_note": (
                    "Shadow candidates only. Do not promote until separate holdout, support, "
                    "loss-rate, and concentration review passes."
                ),
                "cohorts": {},
            }
        }
    }
    cohorts = payload["calibration"]["optuna_shadow_policy_candidates"]["cohorts"]
    for row in recommendations:
        if not str(row.get("recommended_candidate_id") or "").strip():
            continue
        try:
            component_spec = json.loads(str(row.get("component_spec_json") or ""))
            sleeve_weight = json.loads(str(row.get("sleeve_weight_json") or ""))
            gate_spec = json.loads(str(row.get("gate_spec_json") or ""))
        except json.JSONDecodeError:
            continue
        cohorts[row["calibration_cohort"]] = {
            "candidate_id": row["recommended_candidate_id"],
            "promotion_status": row["promotion_status"],
            "objective_value": to_float(row["objective_value"]),
            "pass_fold_rate": to_float(row["pass_fold_rate"]),
            "validation_unique_tickers": int(to_float(row["validation_unique_tickers"]) or 0),
            "mean_loss_rate": to_float(row["mean_loss_rate"]),
            "max_single_ticker_share": to_float(row["max_single_ticker_share"]),
            "max_single_ticker_share_limit": to_float(row["max_single_ticker_share_limit"]),
            "min_lcb_scope": row["min_lcb_scope"],
            "min_lcb_excess_60d": to_float(row["min_lcb_excess_60d"]),
            "min_lcb_excess_120d": to_float(row["min_lcb_excess_120d"]),
            "component_spec": component_spec,
            "sleeve_weight": sleeve_weight,
            "gate_spec": gate_spec,
            "promotion_reason": row["promotion_reason"],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml  # type: ignore

        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
    except ImportError:
        raise RuntimeError("PyYAML is required to write a .yaml Optuna config fragment.") from None


def main() -> None:
    configure_utc_logging()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    args = parse_args()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    input_csv = resolve_output_path(
        cli_value=args.input_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.input_csv",
        default=cfg_get(config, "calibration.cohort_neutral_backtest_csv"),
        base_dir=base_dir,
    )
    policy_csv = resolve_output_path(
        cli_value=args.policy_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.policy_csv",
        default=cfg_get(config, "calibration.component_promotion_review.output_csv"),
        base_dir=base_dir,
    )
    output_csv = resolve_output_path(
        cli_value=args.output_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.output_csv",
        default="../output/med_devices_reports/calibration/med_device_optuna_policy_trials.csv",
        base_dir=base_dir,
    )
    summary_csv = resolve_output_path(
        cli_value=args.summary_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.summary_csv",
        default="../output/med_devices_reports/calibration/med_device_optuna_policy_summary.csv",
        base_dir=base_dir,
    )
    recommendation_csv = resolve_output_path(
        cli_value=args.recommendation_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.recommendation_csv",
        default="../output/med_devices_reports/calibration/med_device_optuna_policy_recommendations.csv",
        base_dir=base_dir,
    )
    fold_diagnostics_csv = resolve_output_path(
        cli_value=args.fold_diagnostics_csv,
        config=config,
        config_path="calibration.optuna_policy_optimizer.fold_diagnostics_csv",
        default="../output/med_devices_reports/calibration/med_device_optuna_policy_fold_diagnostics.csv",
        base_dir=base_dir,
    )
    config_fragment_yaml = resolve_output_path(
        cli_value=args.config_fragment_yaml,
        config=config,
        config_path="calibration.optuna_policy_optimizer.config_fragment_yaml",
        default="../output/med_devices_reports/calibration/med_device_optuna_policy_config_fragment.yaml",
        base_dir=base_dir,
    )
    study_journal_path = resolve_path(
        cfg_get(
            config,
            "calibration.optuna_policy_optimizer.study_journal_path",
            str(output_csv.with_name(output_csv.stem + "_study_journal.log")),
        ),
        base_dir=base_dir,
    )
    study_journal_path.parent.mkdir(parents=True, exist_ok=True)
    horizons = parse_int_list(args.horizons) or parse_int_list(
        cfg_get(config, "calibration.optuna_policy_optimizer.horizons", "60,120")
    )
    horizons = [horizon for horizon in horizons if horizon in {30, 60, 120}]
    if not horizons:
        raise SystemExit("No valid horizons configured for Optuna optimization.")
    settings = build_settings(config, horizons)
    n_trials = args.n_trials_per_cohort or cfg_int(config, "calibration.optuna_policy_optimizer.n_trials_per_cohort", 120)
    timeout_sec = args.timeout_sec_per_cohort
    if timeout_sec is None:
        timeout_sec = cfg_int(config, "calibration.optuna_policy_optimizer.timeout_sec_per_cohort", 0)
    seed = args.seed if args.seed is not None else cfg_int(config, "calibration.optuna_policy_optimizer.seed", 20260607)

    rows, feature_names = load_panel(input_csv)
    if not rows:
        raise SystemExit(f"No panel rows loaded from {input_csv}")
    folds = build_folds(rows, config)
    if not folds:
        raise SystemExit("No walk-forward folds could be built for Optuna optimization.")
    component_candidates = load_component_candidates(policy_csv, config=config, feature_names=feature_names, horizons=set(horizons))
    rows_by_cohort: dict[str, list[PanelRow]] = defaultdict(list)
    for row in rows:
        rows_by_cohort[row.cohort].append(row)
    requested_cohorts = parse_str_set(args.cohorts)
    cohorts = sorted(requested_cohorts or rows_by_cohort.keys())

    all_trial_rows: list[dict[str, Any]] = []
    all_fold_diagnostic_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    recommendation_rows: list[dict[str, Any]] = []
    configured_min_components = cfg_int(config, "calibration.optuna_policy_optimizer.min_components_per_candidate", 2)
    for cohort in cohorts:
        cohort_rows = rows_by_cohort.get(cohort, [])
        candidates = component_candidates.get(cohort, [])
        if not cohort_rows:
            continue
        if not candidates:
            summary_rows.append(
                {
                    "calibration_cohort": cohort,
                    "candidate_count": 0,
                    "completed_candidate_count": 0,
                    "pruned_candidate_count": 0,
                    "best_candidate_id": "",
                    "best_objective_value": "",
                    "best_candidate_status": "no_eligible_components",
                    "best_candidate_reason": "no_promoted_component_candidates_after_filters",
                    "best_pass_fold_rate": "",
                    "best_validation_unique_tickers": "",
                    "best_min_lcb_excess_60d": "",
                    "best_min_lcb_excess_120d": "",
                    "best_mean_loss_rate": "",
                    "best_max_single_ticker_share": "",
                    "best_max_single_ticker_share_limit": "",
                    "best_min_lcb_scope": "",
                    "eligible_component_count": 0,
                    "eligible_components_json": "[]",
                }
            )
            continue
        if len(candidates) < configured_min_components:
            summary_rows.append(
                {
                    "calibration_cohort": cohort,
                    "candidate_count": 0,
                    "completed_candidate_count": 0,
                    "pruned_candidate_count": 0,
                    "best_candidate_id": "",
                    "best_objective_value": "",
                    "best_candidate_status": "insufficient_components",
                    "best_candidate_reason": (
                        f"eligible_components_{len(candidates)}_below_min_components_{configured_min_components}"
                    ),
                    "best_pass_fold_rate": "",
                    "best_validation_unique_tickers": "",
                    "best_min_lcb_excess_60d": "",
                    "best_min_lcb_excess_120d": "",
                    "best_mean_loss_rate": "",
                    "best_max_single_ticker_share": "",
                    "best_max_single_ticker_share_limit": "",
                    "best_min_lcb_scope": "",
                    "eligible_component_count": len(candidates),
                    "eligible_components_json": json_dumps(
                        [
                            {
                                "component": item.component,
                                "direction": item.direction,
                                "sleeve": item.sleeve,
                                "quality": item.quality,
                                "horizons": item.horizons,
                            }
                            for item in candidates
                        ]
                    ),
                }
            )
            continue

        def objective(trial: optuna.Trial) -> float:
            candidate = sample_candidate(trial=trial, cohort=cohort, candidates=candidates, config=config)
            result = evaluate_candidate(
                candidate=candidate,
                cohort_rows=cohort_rows,
                folds=folds,
                horizons=horizons,
                settings=settings,
            )
            trial.set_user_attr("candidate", candidate_to_attr(candidate))
            trial.set_user_attr("result", result)
            prune_reason = infeasible_prune_reason(result, settings)
            if prune_reason:
                result["candidate_status"] = "pruned"
                result["candidate_reason"] = compact_reasons([prune_reason, result.get("candidate_reason", "")])
                trial.set_user_attr("result", result)
                trial.set_user_attr("prune_reason", prune_reason)
                raise optuna.TrialPruned(prune_reason)
            return float(result["objective_value"])

        cohort_seed = seed + int(hashlib.sha1(cohort.encode("utf-8")).hexdigest()[:8], 16) % 100_000
        sampler = optuna.samplers.TPESampler(seed=cohort_seed)
        journal_file_backend_cls: Any = getattr(optuna.storages, "JournalFileBackend", None)
        journal_file_storage_cls: Any = getattr(optuna.storages, "JournalFileStorage", None)
        if journal_file_backend_cls is not None:
            journal_backend = journal_file_backend_cls(str(study_journal_path))
        elif journal_file_storage_cls is not None and sys.platform != "win32":
            journal_backend = journal_file_storage_cls(str(study_journal_path))
        else:
            journal_backend = None
        storage = (
            optuna.storages.JournalStorage(journal_backend)
            if journal_backend is not None
            else optuna.storages.RDBStorage(f"sqlite:///{study_journal_path.with_suffix('.sqlite').as_posix()}")
        )
        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            storage=storage,
            study_name=f"med_device_{cohort}",
            load_if_exists=True,
        )
        study.optimize(objective, n_trials=n_trials, timeout=timeout_sec or None, show_progress_bar=False)
        cohort_trial_rows: list[dict[str, Any]] = []
        for trial in study.trials:
            result = trial.user_attrs.get("result")
            candidate = candidate_from_attr(trial.user_attrs.get("candidate"))
            if not isinstance(result, dict) or candidate is None:
                if trial.state.name == "PRUNED":
                    pruned_row: dict[str, Any] = {field: "" for field in TRIAL_FIELDS}
                    pruned_row.update(
                        {
                            "calibration_cohort": cohort,
                            "trial_number": trial.number,
                            "trial_state": trial.state.name,
                            "prune_reason": trial.user_attrs.get("prune_reason", "pruned_before_candidate_evaluation"),
                            "candidate_id": "pruned_before_candidate_evaluation",
                            "candidate_status": "pruned",
                            "candidate_reason": trial.user_attrs.get("prune_reason", "pruned_before_candidate_evaluation"),
                        }
                    )
                    all_trial_rows.append(pruned_row)
                    cohort_trial_rows.append(pruned_row)
                continue
            candidate_row = trial_row(trial, result, candidate)
            all_trial_rows.append(candidate_row)
            cohort_trial_rows.append(candidate_row)
            for detail in result.get("fold_diagnostics", []):
                diagnostic: dict[str, Any] = {field: "" for field in FOLD_DIAGNOSTIC_FIELDS}
                diagnostic.update(
                    {
                        "calibration_cohort": cohort,
                        "trial_number": trial.number,
                        "trial_state": trial.state.name,
                        "candidate_id": result["candidate_id"],
                        "fold_id": detail["fold_id"],
                        "is_incomplete_fold": detail["is_incomplete_fold"],
                        "counted_in_pass_rate": detail["counted_in_pass_rate"],
                        "horizon_days": detail["horizon_days"],
                        "validation_start": detail["validation_start"],
                        "validation_end": detail["validation_end"],
                        "selected_count": detail["selected_count"],
                        "unique_tickers": detail["unique_tickers"],
                        "ticker_coverage": fmt(detail["ticker_coverage"]),
                        "excess_hit_count": detail["excess_hit_count"],
                        "excess_hit_rate": fmt(detail["excess_hit_rate"]),
                        "excess_hit_p_value": fmt(detail["excess_hit_p_value"]),
                        "loss_rate": fmt(detail["loss_rate"]),
                        "lcb_excess": fmt(detail["lcb_excess"]),
                        "single_ticker_share": fmt(detail["single_ticker_share"]),
                        "fold_horizon_status": detail["fold_horizon_status"],
                        "guardrail_reason": detail["guardrail_reason"],
                        "selected_tickers": detail["selected_tickers"],
                    }
                )
                all_fold_diagnostic_rows.append(diagnostic)
        if cohort_trial_rows:
            summary_rows.append(summarize_cohort(cohort, cohort_trial_rows, candidates))
            best = best_recommendable_row(cohort_trial_rows)
            recommendation_rows.append(recommendation_row(best))

    write_csv(output_csv, all_trial_rows, TRIAL_FIELDS)
    write_csv(fold_diagnostics_csv, all_fold_diagnostic_rows, FOLD_DIAGNOSTIC_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    write_csv(recommendation_csv, recommendation_rows, RECOMMENDATION_FIELDS)
    write_config_fragment(config_fragment_yaml, recommendation_rows)
    print(
        f"Optuna shadow optimization complete: cohorts={len(cohorts)}, trials={len(all_trial_rows)}, "
        f"recommendations={len(recommendation_rows)}"
    )
    print(f"Trials: {output_csv}")
    print(f"Summary: {summary_csv}")
    print(f"Recommendations: {recommendation_csv}")
    print(f"Fold diagnostics: {fold_diagnostics_csv}")
    print(f"Config fragment: {config_fragment_yaml}")


if __name__ == "__main__":
    raise SystemExit(main())
