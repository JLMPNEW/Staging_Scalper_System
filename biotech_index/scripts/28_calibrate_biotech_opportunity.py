#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterable as IterableABC
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.pipeline_guards import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("calibrate_biotech_opportunity")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
ALLOWED_LATEST_TABLES = frozenset(
    {
        "commercial_value_features_daily",
        "forward_guidance_features_daily",
        "governance_event_features_daily",
    }
)

DEFAULT_ROUND_TRIP_COST_BPS = 40.0
DEFAULT_LCB_Z = 1.0
DEFAULT_CVAR_Q = 0.05
DEFAULT_OMEGA_HURDLE = 0.0
DEFAULT_MIN_SELECTED_OBSERVATIONS = 30
DEFAULT_MIN_ASOF_DATES = 8
DEFAULT_MIN_NET_LCB_RETURN_PCT = 0.0
DEFAULT_MIN_SORTINO = 0.0
DEFAULT_MIN_PROFIT_FACTOR = 1.15
DEFAULT_MIN_OMEGA = 1.05
DEFAULT_MAX_BINARY_WEAKNESS_EXPOSURE_PCT = 10.0
DEFAULT_MAX_HARD_WEAKNESS_EXPOSURE_PCT = 10.0
DEFAULT_MAX_CORE_HARD_WEAKNESS_EXPOSURE_PCT = 10.0
DEFAULT_MAX_ILLIQUID_WEAKNESS_EXPOSURE_PCT = 0.0
DEFAULT_LEGACY_BINARY_CONSTRAINT_ENABLED = False
DEFAULT_AGGREGATE_HARD_CONSTRAINT_ENABLED = False
DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT = 55.0
DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT = 30.0
DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT = 12.5
DEFAULT_BOOTSTRAP_ITERATIONS = 200
DEFAULT_BOOTSTRAP_TOP_K = 10
DEFAULT_BOOTSTRAP_SEED = 1729
DEFAULT_HOLDOUT_TOP_K = 25
DEFAULT_SELECTED_TICKER_DIAGNOSTIC_TOP_RANKS = 3
CURRENT_CONFIG_CANDIDATE_NAME = "current_config"
RAW_SCORE_KEYS = [
    "catalyst_score_raw",
    "credibility_score_raw",
    "financial_quality_score_raw",
    "risk_score_raw",
    "momentum_score_raw",
]

CORE_HARD_WEAKNESS_REASONS = frozenset(
    {
        "cash_runway_lt_9m",
        "severe_runway_flag",
        "going_concern_confirmed",
        "reverse_split_history",
        "no_active_trial_no_business_anchor",
        "illiquid",
    }
)
EVENT_HARD_WEAKNESS_REASONS = frozenset({"repeated_dilution", "negative_clinical_event"})

PROFILE_COMPONENTS = [
    "clinical_opportunity",
    "commercial_value",
    "forward_guidance",
    "valuation",
    "upside_capacity",
    "financial_quality",
    "momentum",
    "risk_penalty",
]
SPREAD_KEYS = [
    "mean_return_pct",
    "median_return_pct",
    "winsorized_mean_return_pct",
    "lcb_return_pct",
    "cvar_5_return_pct",
    "sortino_like",
    "profit_factor",
    "omega_configured",
    "p10_return_pct",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "binary_weakness_exposure_pct",
    "hard_weakness_exposure_pct",
    "core_hard_weakness_exposure_pct",
    "event_hard_weakness_exposure_pct",
    "soft_weakness_exposure_pct",
    "normal_binary_exposure_pct",
    "illiquid_weakness_exposure_pct",
    "top3_gain_contribution_pct",
]
BOOTSTRAP_METRIC_KEYS = [
    "mean_return_pct",
    "lcb_return_pct",
    "sortino_like",
    "profit_factor",
    "omega_configured",
    "large_loss_20pct_rate_pct",
    "core_hard_weakness_exposure_pct",
    "event_hard_weakness_exposure_pct",
    "soft_weakness_exposure_pct",
    "illiquid_weakness_exposure_pct",
]


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


@dataclass(frozen=True)
class CalibrationParams:
    round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS
    lcb_z: float = DEFAULT_LCB_Z
    cvar_q: float = DEFAULT_CVAR_Q
    omega_hurdle: float = DEFAULT_OMEGA_HURDLE
    min_selected_observations: int = DEFAULT_MIN_SELECTED_OBSERVATIONS
    min_asof_dates: int = DEFAULT_MIN_ASOF_DATES
    min_net_lcb_return_pct: float = DEFAULT_MIN_NET_LCB_RETURN_PCT
    min_sortino: float = DEFAULT_MIN_SORTINO
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR
    min_omega: float = DEFAULT_MIN_OMEGA
    max_binary_weakness_exposure_pct: float = DEFAULT_MAX_BINARY_WEAKNESS_EXPOSURE_PCT
    max_hard_weakness_exposure_pct: float = DEFAULT_MAX_HARD_WEAKNESS_EXPOSURE_PCT
    max_core_hard_weakness_exposure_pct: float = DEFAULT_MAX_CORE_HARD_WEAKNESS_EXPOSURE_PCT
    max_illiquid_weakness_exposure_pct: float = DEFAULT_MAX_ILLIQUID_WEAKNESS_EXPOSURE_PCT
    legacy_binary_constraint_enabled: bool = DEFAULT_LEGACY_BINARY_CONSTRAINT_ENABLED
    aggregate_hard_constraint_enabled: bool = DEFAULT_AGGREGATE_HARD_CONSTRAINT_ENABLED
    max_top3_gain_contribution_pct: float = DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT
    max_large_loss_20_rate_pct: float = DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT
    max_large_loss_40_rate_pct: float = DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT
    convex_risk_penalty_enabled: bool = True
    risk_penalty_convexity: float = 0.35
    risk_penalty_inflection: float = 50.0


@dataclass(frozen=True)
class ConfidenceParams:
    enabled: bool = False
    min_multiplier: float = 0.82
    low_quality_penalty: float = 0.06
    missing_field_penalty: float = 0.006
    max_missing_penalty: float = 0.08


@dataclass(frozen=True)
class WeightSpec:
    candidate_name: str
    description: str
    clinical_catalyst: float
    clinical_credibility: float
    clinical_financial_quality: float
    clinical_momentum: float
    clinical_risk_penalty: float
    clinical_stage_profile: Mapping[str, float]
    commercial_stage_profile: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "clinical_stage_profile", MappingProxyType(dict(self.clinical_stage_profile)))
        object.__setattr__(self, "commercial_stage_profile", MappingProxyType(dict(self.commercial_stage_profile)))


@dataclass(frozen=True)
class SelectionPolicy:
    policy_name: str
    description: str
    hard_veto: bool = False
    require_liquidity: bool = False
    max_risk_score: float | None = None
    hard_weakness_penalty: float = 0.0
    soft_weakness_penalty: float = 0.0
    illiquid_penalty: float = 0.0
    hard_veto_reasons: tuple[str, ...] = ()
    hard_weakness_penalty_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateGridJob:
    index: int
    horizon: int
    top_n: int
    spec: WeightSpec
    policy: SelectionPolicy


@dataclass(frozen=True)
class BootstrapCiJob:
    index: int
    sample: str
    evaluation_split: str
    horizon: int
    top_n: int
    train_rank: int
    spec: WeightSpec
    policy: SelectionPolicy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bottom-up calibration for biotech_opportunity_score weights. "
            "This script recomputes Tier-1 opportunity score candidates from historical feature snapshots, "
            "then ranks them by net, downside-first calibration metrics. It does not mutate config.yaml."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120", help="Comma-separated trading-day horizons.")
    parser.add_argument("--top-n", type=str, default="10,20,30", help="Comma-separated Top-N cutoffs.")
    parser.add_argument("--market-sources", type=str, default="yahoo_adjusted,interactive_brokers")
    parser.add_argument("--max-snapshots", type=int, default=0, help="Optional smoke-test limit; keeps latest dates.")
    parser.add_argument("--candidate-limit", type=int, default=0, help="Optional smoke-test limit; keeps current config first.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Worker threads for independent candidate-grid and bootstrap jobs. Defaults to calibration.tier1.max_workers or CPU count.",
    )
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=None,
        help=(
            "Date-cluster bootstrap resamples for the CI report. Defaults to "
            f"calibration.tier1.bootstrap_iterations or {DEFAULT_BOOTSTRAP_ITERATIONS}; use 0 to disable."
        ),
    )
    parser.add_argument(
        "--bootstrap-top-k",
        type=int,
        default=None,
        help=(
            "Train-ranked candidates per sample/horizon/top-N group to include in the bootstrap CI report. "
            f"Defaults to calibration.tier1.bootstrap_top_k or {DEFAULT_BOOTSTRAP_TOP_K}."
        ),
    )
    parser.add_argument(
        "--bootstrap-seed",
        type=int,
        default=None,
        help=f"Base random seed for deterministic bootstrap resampling. Defaults to {DEFAULT_BOOTSTRAP_SEED}.",
    )
    parser.add_argument(
        "--holdout-top-k",
        type=int,
        default=None,
        help=(
            "Train-ranked candidates per sample/horizon/top-N group to include in the holdout report. "
            f"Defaults to calibration.tier1.holdout_top_k or {DEFAULT_HOLDOUT_TOP_K}."
        ),
    )
    parser.add_argument(
        "--selected-ticker-diagnostic-top-ranks",
        type=int,
        default=None,
        help=(
            "Train-ranked candidates per group to include in selected ticker diagnostics. Defaults to "
            "calibration.tier1.selected_ticker_diagnostic_top_ranks."
        ),
    )
    parser.add_argument(
        "--train-fraction",
        type=float,
        default=None,
        help="Chronological train fraction for in-sample candidate ranking. Defaults to calibration.tier1.train_fraction or 0.70.",
    )
    parser.add_argument(
        "--strict-feature-lag",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use only commercial/guidance/governance feature rows dated before the Tier-1 feature snapshot. "
            "Default comes from calibration.tier1.strict_feature_lag, otherwise true."
        ),
    )
    parser.add_argument(
        "--next-bar-entry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Enter on the first market bar strictly after the feature snapshot date. "
            "Default comes from calibration.tier1.next_bar_entry, otherwise true."
        ),
    )
    parser.add_argument(
        "--medium-term-horizons",
        type=str,
        default="",
        help="Comma-separated trading-bar horizons to aggregate in the medium-term best report.",
    )
    parser.add_argument("--include-non-fridays", action="store_true", help="Include non-Friday feature snapshots.")
    parser.add_argument(
        "--exclude-current-removals",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Exclude current inactive/remove/manual-exclude companies. Default comes from "
            "calibration.tier1.exclude_current_removals, otherwise false to reduce survivorship bias."
        ),
    )
    parser.add_argument("--exclude-tickers", type=str, default="")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_int_list(raw: str, *, default: list[int]) -> list[int]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        parsed = float(part)
        if not parsed.is_integer():
            raise ValueError(f"Expected integer list value, got {part}")
        value = int(parsed)
        if value <= 0:
            raise ValueError(f"Expected positive integer list value, got {value}")
        values.append(value)
    return values or list(default)


def parse_string_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    return {ticker for part in parts if (ticker := normalize_ticker(part))}


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raw = int(raw)
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def chunked(values: list[Any], size: int = SQLITE_PARAM_CHUNK_SIZE) -> Iterable[list[Any]]:
    step = max(1, int(size))
    for start in range(0, len(values), step):
        yield values[start : start + step]


def run_indexed_jobs(
    jobs: list[Any],
    worker: Callable[[Any], dict[str, Any]],
    *,
    max_workers: int,
    job_label: str,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    if max_workers <= 1 or len(jobs) <= 1:
        return [worker(job) for job in jobs]

    worker_count = max(1, min(int(max_workers), len(jobs)))
    results: dict[int, dict[str, Any]] = {}
    executor = ThreadPoolExecutor(max_workers=worker_count)
    shutdown_wait = True
    future_map: dict[Any, int] = {}
    try:
        future_map = {executor.submit(worker, job): int(job.index) for job in jobs}
        for future in as_completed(future_map):
            job_index = future_map[future]
            try:
                results[job_index] = future.result()
            except Exception:
                shutdown_wait = False
                for pending in future_map:
                    pending.cancel()
                LOGGER.exception("%s job failed: index=%s", job_label, job_index)
                raise
    except KeyboardInterrupt:
        shutdown_wait = False
        for pending in future_map:
            pending.cancel()
        raise
    finally:
        if sys.version_info >= (3, 9):
            executor.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
        else:
            executor.shutdown(wait=shutdown_wait)
    return [results[int(job.index)] for job in sorted(jobs, key=lambda item: int(item.index))]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def load_calibration_params(config: dict[str, Any]) -> CalibrationParams:
    stack = cfg_get(config, "calibration.tier1.recommended_stack", {}) or {}
    costs = cfg_get(config, "calibration.tier1.costs", {}) or {}
    return CalibrationParams(
        round_trip_cost_bps=float(costs.get("long_round_trip_bps", DEFAULT_ROUND_TRIP_COST_BPS)),
        lcb_z=float(stack.get("lcb_z", DEFAULT_LCB_Z)),
        cvar_q=float(stack.get("cvar_q", DEFAULT_CVAR_Q)),
        omega_hurdle=float(stack.get("omega_hurdle", DEFAULT_OMEGA_HURDLE)),
        min_selected_observations=int(stack.get("min_selected_observations", DEFAULT_MIN_SELECTED_OBSERVATIONS)),
        min_asof_dates=int(stack.get("min_asof_dates", DEFAULT_MIN_ASOF_DATES)),
        min_net_lcb_return_pct=float(stack.get("min_net_lcb_return_pct", DEFAULT_MIN_NET_LCB_RETURN_PCT)),
        min_sortino=float(stack.get("min_sortino", DEFAULT_MIN_SORTINO)),
        min_profit_factor=float(stack.get("min_profit_factor", DEFAULT_MIN_PROFIT_FACTOR)),
        min_omega=float(stack.get("min_omega", DEFAULT_MIN_OMEGA)),
        max_binary_weakness_exposure_pct=float(
            stack.get("max_binary_weakness_exposure_pct", DEFAULT_MAX_BINARY_WEAKNESS_EXPOSURE_PCT)
        ),
        max_hard_weakness_exposure_pct=float(
            stack.get("max_hard_weakness_exposure_pct", DEFAULT_MAX_HARD_WEAKNESS_EXPOSURE_PCT)
        ),
        max_core_hard_weakness_exposure_pct=float(
            stack.get("max_core_hard_weakness_exposure_pct", DEFAULT_MAX_CORE_HARD_WEAKNESS_EXPOSURE_PCT)
        ),
        max_illiquid_weakness_exposure_pct=float(
            stack.get("max_illiquid_weakness_exposure_pct", DEFAULT_MAX_ILLIQUID_WEAKNESS_EXPOSURE_PCT)
        ),
        legacy_binary_constraint_enabled=as_bool(
            stack.get("legacy_binary_constraint_enabled", DEFAULT_LEGACY_BINARY_CONSTRAINT_ENABLED),
            DEFAULT_LEGACY_BINARY_CONSTRAINT_ENABLED,
        ),
        aggregate_hard_constraint_enabled=as_bool(
            stack.get("aggregate_hard_constraint_enabled", DEFAULT_AGGREGATE_HARD_CONSTRAINT_ENABLED),
            DEFAULT_AGGREGATE_HARD_CONSTRAINT_ENABLED,
        ),
        max_top3_gain_contribution_pct=float(
            stack.get("max_top3_gain_contribution_pct", DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT)
        ),
        max_large_loss_20_rate_pct=float(
            stack.get("max_large_loss_20_rate_pct", DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT)
        ),
        max_large_loss_40_rate_pct=float(
            stack.get("max_large_loss_40_rate_pct", DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT)
        ),
        convex_risk_penalty_enabled=as_bool(
            cfg_get(
                config,
                "calibration.tier1.convex_risk_penalty_enabled",
                cfg_get(config, "biotech_scoring.convex_risk_penalty_enabled", True),
            ),
            True,
        ),
        risk_penalty_convexity=float(
            cfg_get(
                config,
                "calibration.tier1.risk_penalty_convexity",
                cfg_get(config, "biotech_scoring.risk_penalty_convexity", 0.35),
            )
        ),
        risk_penalty_inflection=float(
            cfg_get(
                config,
                "calibration.tier1.risk_penalty_inflection",
                cfg_get(config, "biotech_scoring.risk_penalty_inflection", 50.0),
            )
        ),
    )


def clamp(value: float | None, low: float = 0.0, high: float = 100.0) -> float:
    parsed = to_float(value, low)
    if parsed is None:
        raise TypeError("clamp: to_float returned None with a non-None default")
    return max(low, min(high, parsed))


def convex_risk_drag(risk: float, weight: float, params: CalibrationParams) -> float:
    base_drag = weight * risk
    if not params.convex_risk_penalty_enabled:
        return base_drag
    inflection = params.risk_penalty_inflection
    excess = max(0.0, risk - inflection) / max(1.0, 100.0 - inflection)
    return base_drag * (1.0 + params.risk_penalty_convexity * excess)


def count_missing_fields(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (list, tuple, set, dict)):
        return len(raw)
    text = str(raw or "").strip()
    if not text or text in {"[]", "{}", "null", "None"}:
        return 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, (list, tuple, set, dict)):
        return len(payload)
    normalized = text.replace(";", ",").replace("|", ",")
    return len([part for part in normalized.split(",") if part.strip()])


def load_confidence_params(config: dict[str, Any]) -> ConfidenceParams:
    return ConfidenceParams(
        enabled=as_bool(cfg_get(config, "biotech_scoring.data_quality_adjustment.enabled", False), False),
        min_multiplier=float(cfg_get(config, "biotech_scoring.data_quality_adjustment.min_multiplier", 0.82)),
        low_quality_penalty=float(cfg_get(config, "biotech_scoring.data_quality_adjustment.low_quality_penalty", 0.06)),
        missing_field_penalty=float(
            cfg_get(config, "biotech_scoring.data_quality_adjustment.missing_field_penalty", 0.006)
        ),
        max_missing_penalty=float(cfg_get(config, "biotech_scoring.data_quality_adjustment.max_missing_penalty", 0.08)),
    )


def score_confidence_multiplier(
    confidence_params: ConfidenceParams,
    payload: dict[str, Any],
    commercial: dict[str, Any],
    forward_guidance: dict[str, Any],
    profile_name: str,
) -> float:
    if not confidence_params.enabled:
        return 1.0

    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    qualities = [str(survival.get("data_quality") or "").lower()]
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    has_guidance = bool(str(forward_guidance.get("latest_guidance_filing_date") or "").strip())
    if profile_name == "commercial_stage" or commercial_stage or profitable:
        qualities.append(str(commercial.get("data_quality") or "").lower())
    if has_guidance:
        qualities.append(str(forward_guidance.get("data_quality") or "").lower())

    low_quality_count = sum(1 for quality in qualities if quality in {"low", "poor", "stale"})
    missing_count = 0
    if profile_name == "commercial_stage" or commercial_stage or profitable:
        missing_count += count_missing_fields(commercial.get("missing_fields"))
    if has_guidance:
        missing_count += count_missing_fields(forward_guidance.get("missing_fields"))
    penalty = low_quality_count * confidence_params.low_quality_penalty + min(
        confidence_params.max_missing_penalty,
        missing_count * confidence_params.missing_field_penalty,
    )
    return clamp(1.0 - penalty, confidence_params.min_multiplier, 1.0)


def profile_name_for(commercial: dict[str, Any], *, revenue_min: float) -> str:
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    ttm_revenue = to_float(commercial.get("ttm_revenue"), 0.0) or 0.0
    return "commercial_stage" if commercial_stage or profitable or ttm_revenue >= revenue_min else "clinical_stage"


def tier1_selection_gate_score(opportunity: float, risk: float) -> float:
    return clamp(0.70 * opportunity + 0.30 * (100.0 - risk))


def normalize_profile(raw: Mapping[str, Any]) -> dict[str, float]:
    profile = {
        "clinical_opportunity": float(raw.get("clinical_opportunity", 0.25)),
        "commercial_value": float(raw.get("commercial_value", raw.get("commercial_quality", 0.25))),
        "forward_guidance": float(raw.get("forward_guidance", 0.0)),
        "valuation": float(raw.get("valuation", 0.20)),
        "upside_capacity": float(raw.get("upside_capacity", 0.10)),
        "financial_quality": float(raw.get("financial_quality", 0.15)),
        "momentum": float(raw.get("momentum", 0.05)),
        "risk_penalty": float(raw.get("risk_penalty", 0.15)),
    }
    for key, value in profile.items():
        if value < 0.0:
            raise ValueError(f"Profile weight '{key}' must be non-negative, got {value}")
    positive_total = sum(value for key, value in profile.items() if key != "risk_penalty")
    if positive_total > 1e-12 and abs(positive_total - 1.0) > 1e-6:
        scale = 1.0 / positive_total
        for key in PROFILE_COMPONENTS:
            if key != "risk_penalty":
                profile[key] *= scale
    return profile


def base_profiles_from_config(config: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    profiles = cfg_get(config, "biotech_scoring.investment_weight_profiles", {}) or {}
    fallback = cfg_get(config, "biotech_scoring.investment_weights", {}) or {}
    clinical = normalize_profile(dict(profiles.get("clinical_stage") or fallback))
    commercial = normalize_profile(dict(profiles.get("commercial_stage") or fallback))
    return clinical, commercial


def profile_signature(profile: Mapping[str, float]) -> tuple[float, ...]:
    return tuple(round(float(profile.get(key, 0.0)), 8) for key in PROFILE_COMPONENTS)


def spec_signature(spec: WeightSpec) -> tuple[Any, ...]:
    return (
        round(spec.clinical_catalyst, 8),
        round(spec.clinical_credibility, 8),
        round(spec.clinical_financial_quality, 8),
        round(spec.clinical_momentum, 8),
        round(spec.clinical_risk_penalty, 8),
        profile_signature(spec.clinical_stage_profile),
        profile_signature(spec.commercial_stage_profile),
    )


def generate_weight_specs(config: dict[str, Any], *, candidate_limit: int = 0) -> list[WeightSpec]:
    weights = cfg_get(config, "biotech_scoring.weights", {}) or {}
    base_catalyst = float(weights.get("catalyst", 0.45))
    base_credibility = float(weights.get("credibility", 0.30))
    base_financial = float(weights.get("financial_quality", 0.15))
    base_momentum = float(weights.get("momentum", 0.10))
    base_risk = float(weights.get("risk_penalty", 0.35))
    base_clinical_profile, base_commercial_profile = base_profiles_from_config(config)

    specs: list[WeightSpec] = [
        WeightSpec(
            candidate_name=CURRENT_CONFIG_CANDIDATE_NAME,
            description="Current biotech_scoring weights from config.yaml.",
            clinical_catalyst=base_catalyst,
            clinical_credibility=base_credibility,
            clinical_financial_quality=base_financial,
            clinical_momentum=base_momentum,
            clinical_risk_penalty=base_risk,
            clinical_stage_profile=base_clinical_profile,
            commercial_stage_profile=base_commercial_profile,
        )
    ]

    clinical_positive_variants = [
        ("current_clinical_mix", base_catalyst, base_credibility, base_financial, base_momentum),
        ("catalyst_heavy", 0.55, 0.25, 0.15, 0.05),
        ("catalyst_credibility", 0.50, 0.35, 0.10, 0.05),
        ("credibility_heavy", 0.35, 0.40, 0.15, 0.10),
        ("quality_balanced", 0.35, 0.30, 0.25, 0.10),
        ("financial_quality_heavy", 0.30, 0.25, 0.35, 0.10),
        ("momentum_light", 0.45, 0.35, 0.15, 0.05),
    ]
    clinical_risk_values = sorted(
        {
            round(max(0.05, base_risk - 0.10), 4),
            round(max(0.05, base_risk - 0.05), 4),
            round(base_risk, 4),
            round(min(0.80, base_risk + 0.05), 4),
            round(min(0.80, base_risk + 0.10), 4),
            round(min(0.80, base_risk + 0.20), 4),
        }
    )
    clinical_stage_variants = [
        ("current_profiles", base_clinical_profile, base_commercial_profile),
        (
            "clinical_anchor",
            normalize_profile(
                {
                    "clinical_opportunity": 0.55,
                    "commercial_value": 0.03,
                    "forward_guidance": 0.03,
                    "valuation": 0.04,
                    "upside_capacity": 0.10,
                    "financial_quality": 0.18,
                    "momentum": 0.07,
                    "risk_penalty": max(base_clinical_profile["risk_penalty"], 0.35),
                }
            ),
            base_commercial_profile,
        ),
        (
            "quality_defensive",
            normalize_profile(
                {
                    "clinical_opportunity": 0.40,
                    "commercial_value": 0.04,
                    "forward_guidance": 0.04,
                    "valuation": 0.05,
                    "upside_capacity": 0.10,
                    "financial_quality": 0.27,
                    "momentum": 0.10,
                    "risk_penalty": max(base_clinical_profile["risk_penalty"], 0.35),
                }
            ),
            base_commercial_profile,
        ),
        (
            "upside_restrained",
            normalize_profile(
                {
                    "clinical_opportunity": 0.45,
                    "commercial_value": 0.05,
                    "forward_guidance": 0.05,
                    "valuation": 0.10,
                    "upside_capacity": 0.05,
                    "financial_quality": 0.20,
                    "momentum": 0.10,
                    "risk_penalty": max(base_clinical_profile["risk_penalty"], 0.35),
                }
            ),
            base_commercial_profile,
        ),
        (
            "clinical_risk_strict",
            normalize_profile(base_clinical_profile | {"risk_penalty": max(base_clinical_profile["risk_penalty"], 0.40)}),
            base_commercial_profile,
        ),
        (
            "commercial_defensive",
            base_clinical_profile,
            normalize_profile(
                {
                    "clinical_opportunity": 0.08,
                    "commercial_value": 0.35,
                    "forward_guidance": 0.15,
                    "valuation": 0.12,
                    "upside_capacity": 0.08,
                    "financial_quality": 0.17,
                    "momentum": 0.05,
                    "risk_penalty": max(base_commercial_profile["risk_penalty"], 0.20),
                }
            ),
        ),
    ]

    seen = {spec_signature(specs[0])}
    for clinical_name, catalyst, credibility, financial, momentum in clinical_positive_variants:
        for risk_penalty in clinical_risk_values:
            for profile_name, clinical_profile, commercial_profile in clinical_stage_variants:
                spec = WeightSpec(
                    candidate_name=f"{clinical_name}_risk{int(round(risk_penalty * 100)):03d}_{profile_name}",
                    description=(
                        f"Clinical mix={clinical_name}; clinical risk penalty={risk_penalty:.2f}; "
                        f"investment profile variant={profile_name}."
                    ),
                    clinical_catalyst=catalyst,
                    clinical_credibility=credibility,
                    clinical_financial_quality=financial,
                    clinical_momentum=momentum,
                    clinical_risk_penalty=risk_penalty,
                    clinical_stage_profile=clinical_profile,
                    commercial_stage_profile=commercial_profile,
                )
                signature = spec_signature(spec)
                if signature in seen:
                    continue
                seen.add(signature)
                specs.append(spec)

    if candidate_limit > 0:
        return specs[: max(1, candidate_limit)]
    return specs


def policy_signature(policy: SelectionPolicy) -> tuple[Any, ...]:
    return (
        policy.policy_name,
        bool(policy.hard_veto),
        bool(policy.require_liquidity),
        None if policy.max_risk_score is None else round(float(policy.max_risk_score), 6),
        round(float(policy.hard_weakness_penalty), 6),
        round(float(policy.soft_weakness_penalty), 6),
        round(float(policy.illiquid_penalty), 6),
        tuple(policy.hard_veto_reasons),
        tuple(policy.hard_weakness_penalty_reasons),
    )


def reason_tuple(raw: object) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, IterableABC):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = part.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


def policy_from_dict(raw: dict[str, Any], *, fallback_name: str) -> SelectionPolicy:
    max_risk_raw = raw.get("max_risk_score")
    max_risk = to_float(max_risk_raw) if max_risk_raw not in {None, ""} else None
    return SelectionPolicy(
        policy_name=str(raw.get("policy_name") or raw.get("name") or fallback_name),
        description=str(raw.get("description") or "Custom calibration selection policy."),
        hard_veto=as_bool(raw.get("hard_veto", False), False),
        require_liquidity=as_bool(raw.get("require_liquidity", False), False),
        max_risk_score=max_risk,
        hard_weakness_penalty=float(raw.get("hard_weakness_penalty", 0.0)),
        soft_weakness_penalty=float(raw.get("soft_weakness_penalty", 0.0)),
        illiquid_penalty=float(raw.get("illiquid_penalty", 0.0)),
        hard_veto_reasons=reason_tuple(raw.get("hard_veto_reasons")),
        hard_weakness_penalty_reasons=reason_tuple(raw.get("hard_weakness_penalty_reasons")),
    )


def generate_selection_policies(config: dict[str, Any]) -> list[SelectionPolicy]:
    raw_policies = cfg_get(config, "calibration.tier1.selection_policies", None)
    policies: list[SelectionPolicy] = []
    if isinstance(raw_policies, list) and raw_policies:
        for idx, raw in enumerate(raw_policies, start=1):
            if isinstance(raw, dict):
                policies.append(policy_from_dict(raw, fallback_name=f"custom_policy_{idx}"))
        if policies:
            return policies

    return [
        SelectionPolicy(
            policy_name="raw_legacy_score",
            description="Legacy score ordering; broad binary weakness remains diagnostic only.",
        ),
        SelectionPolicy(
            policy_name="hard_weakness_veto",
            description="Exclude hard structural weakness; allow normal clinical-stage binary risk.",
            hard_veto=True,
        ),
        SelectionPolicy(
            policy_name="hard_veto_soft_drag",
            description="Exclude hard structural weakness and modestly penalize soft weakness.",
            hard_veto=True,
            soft_weakness_penalty=8.0,
        ),
        SelectionPolicy(
            policy_name="investable_core_risk_cap",
            description="Exclude hard structural weakness, penalize soft weakness, and cap Tier-1 risk at 70.",
            hard_veto=True,
            max_risk_score=70.0,
            soft_weakness_penalty=12.0,
        ),
        SelectionPolicy(
            policy_name="core_structural_veto",
            description="Exclude only core structural hard weakness; allow event/dilution risk as diagnostic exposure.",
            hard_veto=True,
            hard_veto_reasons=tuple(sorted(CORE_HARD_WEAKNESS_REASONS)),
        ),
        SelectionPolicy(
            policy_name="core_veto_event_drag",
            description="Exclude core structural hard weakness and penalize event/dilution hard reasons.",
            hard_veto=True,
            hard_weakness_penalty=10.0,
            hard_veto_reasons=tuple(sorted(CORE_HARD_WEAKNESS_REASONS)),
            hard_weakness_penalty_reasons=tuple(sorted(EVENT_HARD_WEAKNESS_REASONS)),
        ),
        SelectionPolicy(
            policy_name="core_veto_event_soft_drag",
            description="Exclude core structural hard weakness, penalize event/dilution reasons, and apply a soft weakness drag.",
            hard_veto=True,
            hard_weakness_penalty=10.0,
            soft_weakness_penalty=8.0,
            hard_veto_reasons=tuple(sorted(CORE_HARD_WEAKNESS_REASONS)),
            hard_weakness_penalty_reasons=tuple(sorted(EVENT_HARD_WEAKNESS_REASONS)),
        ),
    ]


def policy_fields(policy: SelectionPolicy) -> dict[str, Any]:
    return {
        "selection_policy_name": policy.policy_name,
        "selection_policy_description": policy.description,
        "selection_policy_hard_veto": policy.hard_veto,
        "selection_policy_require_liquidity": policy.require_liquidity,
        "selection_policy_max_risk_score": "" if policy.max_risk_score is None else policy.max_risk_score,
        "selection_policy_hard_weakness_penalty": policy.hard_weakness_penalty,
        "selection_policy_soft_weakness_penalty": policy.soft_weakness_penalty,
        "selection_policy_illiquid_penalty": policy.illiquid_penalty,
        "selection_policy_hard_veto_reasons": "|".join(policy.hard_veto_reasons),
        "selection_policy_hard_weakness_penalty_reasons": "|".join(policy.hard_weakness_penalty_reasons),
    }


def policy_output_keys() -> list[str]:
    return [
        "selection_policy_name",
        "selection_policy_description",
        "selection_policy_hard_veto",
        "selection_policy_require_liquidity",
        "selection_policy_max_risk_score",
        "selection_policy_hard_weakness_penalty",
        "selection_policy_soft_weakness_penalty",
        "selection_policy_illiquid_penalty",
        "selection_policy_hard_veto_reasons",
        "selection_policy_hard_weakness_penalty_reasons",
    ]


def load_snapshot_dates(
    conn: sqlite3.Connection,
    *,
    start_asof: date | None,
    end_asof: date | None,
    fridays_only: bool,
    max_snapshots: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT asof_date
        FROM daily_features
        GROUP BY asof_date
        ORDER BY asof_date
        """
    ).fetchall()
    dates: list[str] = []
    for row in rows:
        parsed = parse_date(row["asof_date"])
        if parsed is None:
            continue
        if start_asof and parsed < start_asof:
            continue
        if end_asof and parsed > end_asof:
            continue
        if fridays_only and parsed.weekday() != 4:
            continue
        dates.append(parsed.isoformat())
    if max_snapshots > 0:
        dates = dates[-max_snapshots:]
    return dates


def load_excluded_tickers(conn: sqlite3.Connection, *, exclude_current_removals: bool, extra: set[str]) -> set[str]:
    out = set(extra)
    if exclude_current_removals:
        rows = conn.execute(
            """
            SELECT ticker
            FROM companies
            WHERE is_active = 0
               OR LOWER(COALESCE(universe_status, '')) = 'remove'
               OR LOWER(COALESCE(manual_exclude, '')) IN ('1', 'true', 't', 'yes', 'y')
            """
        ).fetchall()
        out.update(ticker for row in rows if (ticker := normalize_ticker(row["ticker"])))
    return {ticker for ticker in out if ticker}


def load_feature_rows(conn: sqlite3.Connection, asof_date: str, excluded_tickers: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            f.asof_date, f.company_id, f.catalyst_score_raw, f.credibility_score_raw,
            f.financial_quality_score_raw, f.risk_score_raw, f.momentum_score_raw,
            f.feature_json, c.ticker, c.company_name
        FROM daily_features f
        INNER JOIN companies c ON c.company_id = f.company_id
        WHERE f.asof_date = ?
        ORDER BY c.ticker
        """,
        (asof_date,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker in excluded_tickers:
            continue
        payload = dict(row)
        payload["ticker"] = ticker
        out.append(payload)
    return out


def load_latest_table(
    conn: sqlite3.Connection,
    table: str,
    asof_date: str,
    *,
    strict_prior: bool,
) -> dict[int, dict[str, Any]]:
    if table not in ALLOWED_LATEST_TABLES:
        raise ValueError(f"Unsupported latest-table lookup: {table}")
    if not table_exists(conn, table):
        return {}
    operator = "<" if strict_prior else "<="
    rows = conn.execute(
        f"""
        SELECT t.*
        FROM {table} t
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM {table}
            WHERE asof_date {operator} ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = t.company_id
         AND latest.max_asof = t.asof_date
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def first_float(*values: object) -> float | None:
    for value in values:
        parsed = to_float(value)
        if parsed is not None:
            return parsed
    return None


def raw_score_value(raw_scores: dict[str, Any], row: dict[str, Any], key: str) -> tuple[float, bool]:
    value = first_float(raw_scores.get(key), row.get(key))
    return clamp(value), value is None


def build_binary_weakness_fields(
    payload: dict[str, Any],
    commercial: dict[str, Any],
    governance: dict[str, Any],
    *,
    min_addv20: float,
    risk_score: float,
) -> dict[str, Any]:
    """Split biotech binary risk into hard weakness, soft weakness, and normal clinical binary exposure."""
    ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
    sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}

    addv = first_float(sec_liq.get("median_addv20"), sec_liq.get("avg_dollar_volume_20d"))
    cash_runway = first_float(survival.get("cash_runway_months"))
    reverse_splits = first_float(sec_liq.get("reverse_split_hits_2y"), 0.0) or 0.0
    recent_nt = first_float(sec_liq.get("recent_nt_filing_count_2y"), 0.0) or 0.0
    dilution_events = first_float(sec_events.get("dilution_event_count"), 0.0) or 0.0
    negative_clinical_events = first_float(sec_events.get("negative_clinical_event_count"), 0.0) or 0.0
    verified_active = first_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0) or 0.0
    lead_phase2_3 = first_float(ctgov.get("lead_phase2_3_active_trials"), 0.0) or 0.0
    program_phase2_3 = first_float(ctgov.get("program_phase2_3_active_trials"), 0.0) or 0.0
    active_pivotal = first_float(ctgov.get("active_pivotal_trials"), 0.0) or 0.0
    commercial_stage = first_float(commercial.get("commercial_stage_flag"), 0.0) or 0.0
    profitable = first_float(commercial.get("profitable_flag"), 0.0) or 0.0
    commercial_fragility = first_float(governance.get("commercial_fragility_risk_score"), 0.0) or 0.0
    financial_quality = str(survival.get("data_quality") or "").strip().lower()
    going_concern = str(sec_liq.get("going_concern_status") or "").strip().lower()
    severe_runway = as_bool(survival.get("severe_runway_flag"), False)
    has_business_anchor = commercial_stage > 0.0 or profitable > 0.0
    has_advanced_trial = lead_phase2_3 > 0.0 or program_phase2_3 > 0.0 or active_pivotal > 0.0
    liquidity_ok = addv is not None and addv >= min_addv20

    hard_reasons: list[str] = []
    soft_reasons: list[str] = []
    if cash_runway is not None and cash_runway < 9.0:
        hard_reasons.append("cash_runway_lt_9m")
    elif cash_runway is not None and cash_runway < 12.0 and not has_business_anchor:
        soft_reasons.append("cash_runway_9_to_12m_clinical")
    if severe_runway:
        hard_reasons.append("severe_runway_flag")
    if going_concern == "confirmed":
        hard_reasons.append("going_concern_confirmed")
    elif going_concern in {"possible", "substantial_doubt", "going_concern_warning"}:
        soft_reasons.append("going_concern_warning")
    if reverse_splits > 0:
        hard_reasons.append("reverse_split_history")
    if dilution_events >= 2:
        hard_reasons.append("repeated_dilution")
    elif dilution_events == 1:
        soft_reasons.append("single_dilution_event")
    if negative_clinical_events > 0:
        hard_reasons.append("negative_clinical_event")
    if verified_active <= 0 and not has_business_anchor:
        hard_reasons.append("no_active_trial_no_business_anchor")
    if addv is not None and addv < min_addv20:
        hard_reasons.append("illiquid")
    if financial_quality in {"low", "poor", "stale"}:
        soft_reasons.append("low_financial_data_quality")
    if commercial_fragility >= 70.0:
        soft_reasons.append("high_commercial_fragility")
    if risk_score >= 75.0:
        soft_reasons.append("high_tier1_risk_score")
    if recent_nt > 0:
        soft_reasons.append("recent_nt_filing")
    if verified_active > 0 and not has_advanced_trial and not has_business_anchor:
        soft_reasons.append("early_stage_or_unadvanced_trial_anchor")

    core_hard_reasons = [reason for reason in hard_reasons if reason in CORE_HARD_WEAKNESS_REASONS]
    event_hard_reasons = [reason for reason in hard_reasons if reason in EVENT_HARD_WEAKNESS_REASONS]
    other_hard_reasons = [
        reason
        for reason in hard_reasons
        if reason not in CORE_HARD_WEAKNESS_REASONS and reason not in EVENT_HARD_WEAKNESS_REASONS
    ]
    legacy_reasons = [*hard_reasons, *soft_reasons]
    normal_binary = bool(verified_active > 0 and not has_business_anchor and not hard_reasons)
    if core_hard_reasons and event_hard_reasons:
        severity = "core_event_hard"
    elif normal_binary and not soft_reasons:
        severity = "normal_clinical_binary"
    elif core_hard_reasons:
        severity = "core_hard"
    elif event_hard_reasons:
        severity = "event_hard"
    elif other_hard_reasons:
        severity = "hard"
    elif soft_reasons:
        severity = "soft_only"
    else:
        severity = "none"

    return {
        "diag_avg_dollar_volume_20d": addv if addv is not None else "",
        "diag_liquidity_ok": 1.0 if liquidity_ok else 0.0 if addv is not None else "",
        "diag_cash_runway_months": cash_runway if cash_runway is not None else "",
        "diag_commercial_fragility_risk_score": commercial_fragility,
        "diag_verified_active_trial_count": verified_active,
        "diag_has_business_anchor": 1.0 if has_business_anchor else 0.0,
        "diag_has_advanced_trial_anchor": 1.0 if has_advanced_trial else 0.0,
        "diag_binary_weakness_severity": severity,
        "diag_binary_weakness_count": float(len(legacy_reasons)),
        "diag_binary_weakness_flag": 1.0 if legacy_reasons else 0.0,
        "diag_binary_weakness_reasons": "|".join(legacy_reasons),
        "diag_hard_weakness_count": float(len(hard_reasons)),
        "diag_hard_weakness_flag": 1.0 if hard_reasons else 0.0,
        "diag_hard_weakness_reasons": "|".join(hard_reasons),
        "diag_core_hard_weakness_count": float(len(core_hard_reasons)),
        "diag_core_hard_weakness_flag": 1.0 if core_hard_reasons else 0.0,
        "diag_core_hard_weakness_reasons": "|".join(core_hard_reasons),
        "diag_event_hard_weakness_count": float(len(event_hard_reasons)),
        "diag_event_hard_weakness_flag": 1.0 if event_hard_reasons else 0.0,
        "diag_event_hard_weakness_reasons": "|".join(event_hard_reasons),
        "diag_soft_weakness_count": float(len(soft_reasons)),
        "diag_soft_weakness_flag": 1.0 if soft_reasons else 0.0,
        "diag_soft_weakness_reasons": "|".join(soft_reasons),
        "diag_normal_clinical_binary_flag": 1.0 if normal_binary else 0.0,
        "diag_illiquid_weakness_flag": 1.0 if "illiquid" in hard_reasons else 0.0,
    }


def load_observations(
    conn: sqlite3.Connection,
    dates: list[str],
    excluded_tickers: set[str],
    config: dict[str, Any],
    *,
    min_addv20: float,
    strict_feature_lag: bool,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    revenue_min = float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000.0))
    confidence_params = load_confidence_params(config)
    for asof_date in dates:
        features = load_feature_rows(conn, asof_date, excluded_tickers)
        commercial_by_company = load_latest_table(
            conn,
            "commercial_value_features_daily",
            asof_date,
            strict_prior=strict_feature_lag,
        )
        forward_by_company = load_latest_table(
            conn,
            "forward_guidance_features_daily",
            asof_date,
            strict_prior=strict_feature_lag,
        )
        governance_by_company = load_latest_table(
            conn,
            "governance_event_features_daily",
            asof_date,
            strict_prior=strict_feature_lag,
        )
        for row in features:
            company_id = int(row["company_id"])
            payload = parse_json(row.get("feature_json"))
            raw_scores = payload.get("raw_scores", {}) if isinstance(payload, dict) else {}
            commercial = commercial_by_company.get(company_id, {})
            forward = forward_by_company.get(company_id, {})
            governance = governance_by_company.get(company_id, {})
            profile_name = profile_name_for(commercial, revenue_min=revenue_min)
            confidence = score_confidence_multiplier(confidence_params, payload, commercial, forward, profile_name)
            raw_score_values: dict[str, float] = {}
            missing_raw_score_fields: list[str] = []
            for score_key in RAW_SCORE_KEYS:
                value, missing = raw_score_value(raw_scores, dict(row), score_key)
                raw_score_values[score_key] = value
                if missing:
                    missing_raw_score_fields.append(score_key)
            observation = {
                "asof_date": str(row["asof_date"]),
                "company_id": company_id,
                "ticker": normalize_ticker(row["ticker"]),
                "company_name": str(row.get("company_name") or ""),
                "profile_name": profile_name,
                "confidence_multiplier": confidence,
                **raw_score_values,
                "diag_raw_score_missing_count": float(len(missing_raw_score_fields)),
                "diag_raw_score_missing_flag": 1.0 if missing_raw_score_fields else 0.0,
                "diag_raw_score_missing_fields": "|".join(missing_raw_score_fields),
                "commercial_value_score": clamp(to_float(commercial.get("commercial_value_score"), 35.0)),
                "forward_guidance_score": clamp(to_float(forward.get("guidance_score"), 45.0)),
                "valuation_score": clamp(to_float(commercial.get("valuation_score"), 50.0)),
                "upside_capacity_score": clamp(to_float(commercial.get("upside_capacity_score"), 50.0)),
            }
            observation.update(
                build_binary_weakness_fields(
                    payload,
                    commercial,
                    governance,
                    min_addv20=min_addv20,
                    risk_score=float(observation["risk_score_raw"]),
                )
            )
            observations.append(observation)
    return observations


def load_bars(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    min_date: date,
    market_sources: list[str],
) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    source_priority = {source: idx for idx, source in enumerate(market_sources)}
    grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
    ordered_sources = [source for source in market_sources if source]
    ordered_tickers = sorted(tickers)
    ticker_chunk_size = max(1, SQLITE_PARAM_CHUNK_SIZE - len(ordered_sources) - 1)
    for ticker_chunk in chunked(ordered_tickers, ticker_chunk_size):
        ticker_placeholders = ",".join("?" for _ in ticker_chunk)
        source_placeholders = ",".join("?" for _ in ordered_sources)
        rows = conn.execute(
            f"""
            SELECT ticker, bar_date, source, close
            FROM market_bars_daily
            WHERE ticker IN ({ticker_placeholders})
              AND source IN ({source_placeholders})
              AND bar_date >= ?
              AND close IS NOT NULL
              AND close > 0
            ORDER BY ticker, source, bar_date
            """,
            (*ticker_chunk, *ordered_sources, min_date.isoformat()),
        ).fetchall()
        for row in rows:
            parsed = parse_date(row["bar_date"])
            close = to_float(row["close"])
            if parsed is None or close is None or close <= 0:
                continue
            ticker = normalize_ticker(row["ticker"])
            if ticker:
                grouped[(ticker, str(row["source"] or ""))].append(Bar(day=parsed, close=close))
    out: dict[str, list[Bar]] = {}
    by_ticker: dict[str, list[tuple[int, list[Bar]]]] = defaultdict(list)
    for (group_ticker, source), bars in grouped.items():
        if bars:
            by_ticker[group_ticker].append((source_priority.get(source, 9999), bars))
    for ticker in ordered_tickers:
        candidates = by_ticker.get(ticker, [])
        if candidates:
            out[ticker] = sorted(min(candidates, key=lambda item: item[0])[1], key=lambda bar: bar.day)
    return out


def forward_return(
    bars: list[Bar],
    asof: date,
    horizon: int,
    *,
    next_bar_entry: bool,
) -> tuple[float | None, str, str]:
    if not bars:
        return None, "", ""
    days = [bar.day for bar in bars]
    entry_idx = bisect.bisect_right(days, asof) if next_bar_entry else bisect.bisect_right(days, asof) - 1
    if entry_idx < 0 or entry_idx >= len(bars):
        return None, "", ""
    target_idx = entry_idx + horizon
    if target_idx >= len(bars):
        return None, bars[entry_idx].day.isoformat(), ""
    entry = bars[entry_idx]
    target = bars[target_idx]
    if entry.close <= 0:
        return None, entry.day.isoformat(), target.day.isoformat()
    return (target.close / entry.close) - 1.0, entry.day.isoformat(), target.day.isoformat()


def add_forward_returns(
    rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    horizons: list[int],
    *,
    round_trip_cost_bps: float,
    next_bar_entry: bool,
) -> None:
    cost = float(round_trip_cost_bps) / 10_000.0
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = parse_date(row.get("asof_date"))
        bars = bars_by_ticker.get(ticker, [])
        for horizon in horizons:
            prefix = f"fwd_{horizon}d"
            if asof is None:
                row[f"{prefix}_return"] = ""
                row[f"{prefix}_net_return"] = ""
                row[f"{prefix}_entry_date"] = ""
                row[f"{prefix}_target_date"] = ""
                continue
            ret, entry_date, target_date = forward_return(
                bars,
                asof,
                horizon,
                next_bar_entry=next_bar_entry,
            )
            row[f"{prefix}_return"] = ret if ret is not None else ""
            row[f"{prefix}_net_return"] = ret - cost if ret is not None else ""
            row[f"{prefix}_entry_date"] = entry_date
            row[f"{prefix}_target_date"] = target_date


def score_observation(row: dict[str, Any], spec: WeightSpec, params: CalibrationParams) -> dict[str, float]:
    catalyst = clamp(to_float(row.get("catalyst_score_raw")))
    credibility = clamp(to_float(row.get("credibility_score_raw")))
    financial_quality = clamp(to_float(row.get("financial_quality_score_raw")))
    risk = clamp(to_float(row.get("risk_score_raw")))
    momentum = clamp(to_float(row.get("momentum_score_raw")))
    clinical_positive = (
        spec.clinical_catalyst * catalyst
        + spec.clinical_credibility * credibility
        + spec.clinical_financial_quality * financial_quality
        + spec.clinical_momentum * momentum
    )
    clinical_risk_drag = convex_risk_drag(risk, spec.clinical_risk_penalty, params)
    clinical_opportunity = clamp(clinical_positive - clinical_risk_drag)
    profile = spec.commercial_stage_profile if row.get("profile_name") == "commercial_stage" else spec.clinical_stage_profile
    investment_positive = (
        profile["clinical_opportunity"] * clinical_opportunity
        + profile["commercial_value"] * clamp(to_float(row.get("commercial_value_score")))
        + profile["forward_guidance"] * clamp(to_float(row.get("forward_guidance_score")))
        + profile["valuation"] * clamp(to_float(row.get("valuation_score")))
        + profile["upside_capacity"] * clamp(to_float(row.get("upside_capacity_score")))
        + profile["financial_quality"] * financial_quality
        + profile["momentum"] * momentum
    )
    investment_risk_drag = convex_risk_drag(risk, profile["risk_penalty"], params)
    confidence = clamp(to_float(row.get("confidence_multiplier"), 1.0), 0.0, 1.0)
    pre_confidence_opportunity_score = clamp(investment_positive - investment_risk_drag)
    investment_score = clamp(pre_confidence_opportunity_score * confidence)
    gate_score = tier1_selection_gate_score(investment_score, risk)
    return {
        "clinical_opportunity_score": clinical_opportunity,
        "pre_confidence_opportunity_score": pre_confidence_opportunity_score,
        "investment_score": investment_score,
        "tier1_selection_gate_score": gate_score,
    }


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def pct(value: float | None) -> float | str:
    return "" if value is None else round(100.0 * value, 6)


def rounded(value: float | None, digits: int = 6) -> float | str:
    return "" if value is None else round(value, digits)


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) <= 1e-12:
        return None
    return numerator / denominator


def winsorized_mean(values: list[float], lower_q: float = 0.05, upper_q: float = 0.95) -> float | None:
    if not values:
        return None
    lower = quantile(values, lower_q)
    upper = quantile(values, upper_q)
    if lower is None or upper is None:
        return None
    return mean([min(max(value, lower), upper) for value in values])


def lower_confidence_bound(values: list[float], *, z: float) -> float | None:
    avg = mean(values)
    if avg is None:
        return None
    sigma = stdev(values)
    if sigma is None:
        return avg
    return avg - max(0.0, z) * sigma / math.sqrt(float(len(values)))


def cvar_left_tail(values: list[float], *, q: float) -> float | None:
    cutoff = quantile(values, max(0.001, min(0.50, q)))
    if cutoff is None:
        return None
    tail = [value for value in values if value <= cutoff]
    return mean(tail) if tail else cutoff


def profit_factor(values: list[float], *, hurdle: float = 0.0) -> float | None:
    gains = [max(value - hurdle, 0.0) for value in values]
    losses = [max(hurdle - value, 0.0) for value in values]
    total_loss = sum(losses)
    total_gain = sum(gains)
    if total_loss <= 1e-12:
        return None if total_gain <= 1e-12 else 999.0
    return total_gain / total_loss


def omega_ratio(values: list[float], *, hurdle: float = 0.0) -> float | None:
    return profit_factor(values, hurdle=hurdle)


def top_gain_contribution(values: list[float], *, top_n: int) -> float | None:
    positives = sorted([value for value in values if value > 0.0], reverse=True)
    if not positives:
        return 0.0
    total_positive = sum(positives)
    if total_positive <= 1e-12:
        return 0.0
    return sum(positives[: max(1, top_n)]) / total_positive


def numeric_values(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(key))
        if value is not None:
            values.append(value)
    return values


def mean_numeric(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    return rounded(mean(numeric_values(rows, key)))


def weighted_mean_numeric(rows: Iterable[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        value = to_float(row.get(value_key))
        weight = to_float(row.get(weight_key), 0.0)
        if value is None or weight is None or weight <= 0.0:
            continue
        numerator += value * weight
        denominator += weight
    return numerator / denominator if denominator > 0.0 else None


def pct_flag(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(key))
        if value is not None:
            values.append(1.0 if value > 0.0 else 0.0)
    return round(100.0 * sum(values) / len(values), 6) if values else ""


def summarize_return_risk(values: list[float], *, params: CalibrationParams) -> dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "mean_return_pct": "",
            "median_return_pct": "",
            "hit_rate_pct": "",
            "loss_rate_pct": "",
            "winsorized_mean_return_pct": "",
            "stdev_return_pct": "",
            "downside_deviation_pct": "",
            "lcb_return_pct": "",
            "cvar_5_return_pct": "",
            "sharpe_like": "",
            "sortino_like": "",
            "profit_factor": "",
            "omega_configured": "",
            "omega_0": "",
            "top3_gain_contribution_pct": "",
            "worst_return_pct": "",
            "best_return_pct": "",
            "p05_return_pct": "",
            "p10_return_pct": "",
            "large_loss_20pct_rate_pct": "",
            "large_loss_40pct_rate_pct": "",
            "large_gain_20pct_rate_pct": "",
        }
    positives = [value for value in values if value > 0.0]
    negatives = [value for value in values if value < 0.0]
    downside_terms = [min(0.0, value - params.omega_hurdle) for value in values]
    avg = mean(values)
    volatility = stdev(values)
    downside = math.sqrt(sum(value**2 for value in downside_terms) / len(values))
    lcb = lower_confidence_bound(values, z=params.lcb_z)
    cvar = cvar_left_tail(values, q=params.cvar_q)
    omega_value = rounded(omega_ratio(values, hurdle=params.omega_hurdle))
    return {
        "n": len(values),
        "mean_return_pct": pct(avg),
        "median_return_pct": pct(median(values)),
        "hit_rate_pct": round(100.0 * len(positives) / len(values), 6),
        "loss_rate_pct": round(100.0 * len(negatives) / len(values), 6),
        "winsorized_mean_return_pct": pct(winsorized_mean(values)),
        "stdev_return_pct": pct(volatility),
        "downside_deviation_pct": pct(downside),
        "lcb_return_pct": pct(lcb),
        "cvar_5_return_pct": pct(cvar),
        "sharpe_like": rounded(safe_ratio(avg, volatility)),
        "sortino_like": rounded(safe_ratio(avg, downside)),
        "profit_factor": rounded(profit_factor(values, hurdle=0.0)),
        "omega_configured": omega_value,
        "omega_0": omega_value,
        "top3_gain_contribution_pct": pct(top_gain_contribution(values, top_n=3)),
        "worst_return_pct": pct(min(values)),
        "best_return_pct": pct(max(values)),
        "p05_return_pct": pct(quantile(values, 0.05)),
        "p10_return_pct": pct(quantile(values, 0.10)),
        "large_loss_20pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.20) / len(values), 6),
        "large_loss_40pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.40) / len(values), 6),
        "large_gain_20pct_rate_pct": round(100.0 * sum(1 for value in values if value >= 0.20) / len(values), 6),
    }


def selection_quality_summary(
    rows: list[dict[str, Any]],
    ret_key: str,
    *,
    params: CalibrationParams,
) -> dict[str, Any]:
    returns = numeric_values(rows, ret_key)
    summary = summarize_return_risk(returns, params=params)
    summary.update(
        {
            "binary_weakness_exposure_pct": pct_flag(rows, "diag_binary_weakness_flag"),
            "hard_weakness_exposure_pct": pct_flag(rows, "diag_hard_weakness_flag"),
            "core_hard_weakness_exposure_pct": pct_flag(rows, "diag_core_hard_weakness_flag"),
            "event_hard_weakness_exposure_pct": pct_flag(rows, "diag_event_hard_weakness_flag"),
            "soft_weakness_exposure_pct": pct_flag(rows, "diag_soft_weakness_flag"),
            "normal_binary_exposure_pct": pct_flag(rows, "diag_normal_clinical_binary_flag"),
            "illiquid_weakness_exposure_pct": pct_flag(rows, "diag_illiquid_weakness_flag"),
            "avg_binary_weakness_count": mean_numeric(rows, "diag_binary_weakness_count"),
            "avg_hard_weakness_count": mean_numeric(rows, "diag_hard_weakness_count"),
            "avg_core_hard_weakness_count": mean_numeric(rows, "diag_core_hard_weakness_count"),
            "avg_event_hard_weakness_count": mean_numeric(rows, "diag_event_hard_weakness_count"),
            "avg_soft_weakness_count": mean_numeric(rows, "diag_soft_weakness_count"),
            "liquidity_ok_pct": pct_flag(rows, "diag_liquidity_ok"),
            "raw_score_missing_exposure_pct": pct_flag(rows, "diag_raw_score_missing_flag"),
            "avg_raw_score_missing_count": mean_numeric(rows, "diag_raw_score_missing_count"),
            "mean_risk_score": mean_numeric(rows, "risk_score_raw"),
            "mean_cash_runway_months": mean_numeric(rows, "diag_cash_runway_months"),
            "mean_commercial_fragility_risk_score": mean_numeric(rows, "diag_commercial_fragility_risk_score"),
        }
    )
    return summary


def prefixed(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}{key}": value for key, value in values.items()}


def summary_metric_spread(left: dict[str, Any], right: dict[str, Any], key: str) -> float | str:
    left_value = to_float(left.get(key))
    right_value = to_float(right.get(key))
    return rounded(left_value - right_value) if left_value is not None and right_value is not None else ""


def unprefix(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {key[len(prefix) :]: value for key, value in row.items() if key.startswith(prefix)}


def calibration_constraint_fields(
    selected_summary: dict[str, Any],
    *,
    asof_dates: int,
    params: CalibrationParams,
) -> dict[str, Any]:
    reasons: list[str] = []
    n = int(to_float(selected_summary.get("n"), 0.0) or 0)
    lcb = to_float(selected_summary.get("lcb_return_pct"))
    sortino = to_float(selected_summary.get("sortino_like"))
    profit = to_float(selected_summary.get("profit_factor"))
    omega = to_float(selected_summary.get("omega_configured"), to_float(selected_summary.get("omega_0")))
    binary = to_float(selected_summary.get("binary_weakness_exposure_pct"))
    hard = to_float(selected_summary.get("hard_weakness_exposure_pct"))
    core_hard = to_float(selected_summary.get("core_hard_weakness_exposure_pct"))
    illiquid = to_float(selected_summary.get("illiquid_weakness_exposure_pct"))
    top3 = to_float(selected_summary.get("top3_gain_contribution_pct"))
    loss20 = to_float(selected_summary.get("large_loss_20pct_rate_pct"))
    loss40 = to_float(selected_summary.get("large_loss_40pct_rate_pct"))
    if n < params.min_selected_observations:
        reasons.append(f"n<{params.min_selected_observations}")
    if asof_dates < params.min_asof_dates:
        reasons.append(f"asof_dates<{params.min_asof_dates}")
    if lcb is None or lcb < params.min_net_lcb_return_pct:
        reasons.append(f"lcb<{params.min_net_lcb_return_pct}")
    if sortino is None or sortino < params.min_sortino:
        reasons.append(f"sortino<{params.min_sortino}")
    if profit is None or profit < params.min_profit_factor:
        reasons.append(f"profit_factor<{params.min_profit_factor}")
    omega_is_distinct = abs(float(params.omega_hurdle)) > 1e-12
    if omega_is_distinct and (omega is None or omega < params.min_omega):
        reasons.append(f"omega<{params.min_omega}")
    if params.legacy_binary_constraint_enabled and binary is not None and binary > params.max_binary_weakness_exposure_pct:
        reasons.append(f"legacy_binary_weakness>{params.max_binary_weakness_exposure_pct}")
    if core_hard is not None and core_hard > params.max_core_hard_weakness_exposure_pct:
        reasons.append(f"core_hard_weakness>{params.max_core_hard_weakness_exposure_pct}")
    if params.aggregate_hard_constraint_enabled and hard is not None and hard > params.max_hard_weakness_exposure_pct:
        reasons.append(f"hard_weakness>{params.max_hard_weakness_exposure_pct}")
    if illiquid is not None and illiquid > params.max_illiquid_weakness_exposure_pct:
        reasons.append(f"illiquid_weakness>{params.max_illiquid_weakness_exposure_pct}")
    if top3 is not None and top3 > params.max_top3_gain_contribution_pct:
        reasons.append(f"top3_concentration>{params.max_top3_gain_contribution_pct}")
    if loss20 is not None and loss20 > params.max_large_loss_20_rate_pct:
        reasons.append(f"loss20>{params.max_large_loss_20_rate_pct}")
    if loss40 is not None and loss40 > params.max_large_loss_40_rate_pct:
        reasons.append(f"loss40>{params.max_large_loss_40_rate_pct}")
    return {
        "calibration_pass": not reasons,
        "calibration_fail_reasons": "|".join(reasons),
    }


def robust_objective(selected: dict[str, Any], baseline: dict[str, Any], *, params: CalibrationParams) -> float:
    lcb_spread = to_float(summary_metric_spread(selected, baseline, "lcb_return_pct"), 0.0) or 0.0
    sortino_spread = to_float(summary_metric_spread(selected, baseline, "sortino_like"), 0.0) or 0.0
    profit_spread = to_float(summary_metric_spread(selected, baseline, "profit_factor"), 0.0) or 0.0
    omega_spread = to_float(summary_metric_spread(selected, baseline, "omega_configured"), 0.0) or 0.0
    omega_is_distinct = abs(float(params.omega_hurdle)) > 1e-12
    mean_spread = to_float(summary_metric_spread(selected, baseline, "mean_return_pct"), 0.0) or 0.0
    p10_spread = to_float(summary_metric_spread(selected, baseline, "p10_return_pct"), 0.0) or 0.0
    cvar_spread = to_float(summary_metric_spread(selected, baseline, "cvar_5_return_pct"), 0.0) or 0.0
    loss20_spread = to_float(summary_metric_spread(selected, baseline, "large_loss_20pct_rate_pct"), 0.0) or 0.0
    loss40_spread = to_float(summary_metric_spread(selected, baseline, "large_loss_40pct_rate_pct"), 0.0) or 0.0
    core_hard_spread = (
        to_float(summary_metric_spread(selected, baseline, "core_hard_weakness_exposure_pct"), 0.0) or 0.0
    )
    event_hard_spread = (
        to_float(summary_metric_spread(selected, baseline, "event_hard_weakness_exposure_pct"), 0.0) or 0.0
    )
    soft_spread = to_float(summary_metric_spread(selected, baseline, "soft_weakness_exposure_pct"), 0.0) or 0.0
    illiquid_spread = to_float(summary_metric_spread(selected, baseline, "illiquid_weakness_exposure_pct"), 0.0) or 0.0
    top3_spread = to_float(summary_metric_spread(selected, baseline, "top3_gain_contribution_pct"), 0.0) or 0.0
    return (
        0.12 * lcb_spread
        + 0.50 * sortino_spread
        + 0.20 * profit_spread
        + (0.15 * omega_spread if omega_is_distinct else 0.0)
        + 0.01 * mean_spread
        + 0.01 * p10_spread
        + 0.01 * cvar_spread
        - 0.03 * max(0.0, loss20_spread)
        - 0.05 * max(0.0, loss40_spread)
        - 0.10 * max(0.0, core_hard_spread)
        - 0.025 * max(0.0, event_hard_spread)
        - 0.03 * max(0.0, illiquid_spread)
        - 0.015 * max(0.0, soft_spread)
        - 0.01 * max(0.0, top3_spread)
    )


def numeric_or_default(raw: object, default: float) -> float:
    value = to_float(raw)
    return value if value is not None else default


def calibration_sort_tuple(row: dict[str, Any]) -> tuple[float, float, float, float, float, float, float]:
    passed = 1.0 if as_bool(row.get("calibration_pass")) else 0.0
    objective = numeric_or_default(row.get("calibration_objective_vs_current_config"), -1e9)
    lcb = numeric_or_default(row.get("selected_lcb_return_pct"), -1e9)
    sortino = numeric_or_default(row.get("selected_sortino_like"), -1e9)
    profit = numeric_or_default(row.get("selected_profit_factor"), -1e9)
    core_hard = numeric_or_default(row.get("selected_core_hard_weakness_exposure_pct"), 100.0)
    loss20 = numeric_or_default(row.get("selected_large_loss_20pct_rate_pct"), 100.0)
    return (passed, objective, lcb, sortino, profit, -core_hard, -loss20)


def stable_weight_spec_id(spec: WeightSpec) -> str:
    digest = hashlib.sha1(repr(spec_signature(spec)).encode("ascii")).hexdigest()[:12]
    return f"tier1_weight_{digest}"


def stable_candidate_id(spec: WeightSpec, policy: SelectionPolicy) -> str:
    policy_key = policy_signature(policy)
    digest = hashlib.sha1(f"{repr(spec_signature(spec))}|{repr(policy_key)}".encode("ascii")).hexdigest()[:12]
    return f"tier1_{digest}"


def spec_fields(spec: WeightSpec, policy: SelectionPolicy | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "candidate_id": stable_candidate_id(spec, policy) if policy is not None else stable_weight_spec_id(spec),
        "candidate_name": spec.candidate_name,
        "candidate_description": spec.description,
        "clinical_catalyst_weight": spec.clinical_catalyst,
        "clinical_credibility_weight": spec.clinical_credibility,
        "clinical_financial_quality_weight": spec.clinical_financial_quality,
        "clinical_momentum_weight": spec.clinical_momentum,
        "clinical_risk_penalty_weight": spec.clinical_risk_penalty,
    }
    for profile_name, profile in [
        ("clinical_stage", spec.clinical_stage_profile),
        ("commercial_stage", spec.commercial_stage_profile),
    ]:
        for key in PROFILE_COMPONENTS:
            out[f"{profile_name}_{key}_weight"] = profile.get(key, "")
    if policy is not None:
        out.update(policy_fields(policy))
    return out


def spec_output_keys() -> list[str]:
    keys = [
        "candidate_id",
        "candidate_name",
        "candidate_description",
        "clinical_catalyst_weight",
        "clinical_credibility_weight",
        "clinical_financial_quality_weight",
        "clinical_momentum_weight",
        "clinical_risk_penalty_weight",
    ]
    for profile_name in ["clinical_stage", "commercial_stage"]:
        for key in PROFILE_COMPONENTS:
            keys.append(f"{profile_name}_{key}_weight")
    keys.extend(policy_output_keys())
    return keys


def policy_adjusted_score(
    row: dict[str, Any],
    spec: WeightSpec,
    policy: SelectionPolicy,
    params: CalibrationParams,
) -> tuple[float | None, dict[str, float]]:
    scores = score_observation(row, spec, params)
    risk = clamp(to_float(row.get("risk_score_raw")))
    if policy.max_risk_score is not None and risk > policy.max_risk_score:
        return None, scores
    hard_reasons = set(reason_tuple(row.get("diag_hard_weakness_reasons")))
    if hard_reasons:
        hard_veto_match = (
            bool(hard_reasons.intersection(policy.hard_veto_reasons))
            if policy.hard_veto_reasons
            else True
        )
        hard_penalty_match = (
            bool(hard_reasons.intersection(policy.hard_weakness_penalty_reasons))
            if policy.hard_weakness_penalty_reasons
            else True
        )
    else:
        hard_veto_match = False
        hard_penalty_match = False
    soft = 1.0 if (to_float(row.get("diag_soft_weakness_flag"), 0.0) or 0.0) > 0.0 else 0.0
    illiquid = 1.0 if (to_float(row.get("diag_illiquid_weakness_flag"), 0.0) or 0.0) > 0.0 else 0.0
    liquidity_ok = to_float(row.get("diag_liquidity_ok"))
    if policy.hard_veto and hard_veto_match:
        return None, scores
    if policy.require_liquidity and liquidity_ok != 1.0:
        return None, scores
    adjusted = (
        scores["investment_score"]
        - policy.hard_weakness_penalty * (1.0 if hard_penalty_match else 0.0)
        - policy.soft_weakness_penalty * soft
        - policy.illiquid_penalty * illiquid
    )
    return clamp(adjusted), scores


def annotate_selected_row(
    row: dict[str, Any],
    *,
    candidate_score: float,
    scores: dict[str, float],
    policy: SelectionPolicy,
) -> dict[str, Any]:
    out = dict(row)
    out["candidate_selection_score"] = round(candidate_score, 6)
    out["candidate_pre_confidence_opportunity_score"] = round(scores.get("pre_confidence_opportunity_score", 0.0), 6)
    out["candidate_investment_score"] = round(scores.get("investment_score", 0.0), 6)
    out["candidate_clinical_opportunity_score"] = round(scores.get("clinical_opportunity_score", 0.0), 6)
    out["selection_policy_name"] = policy.policy_name
    return out


def select_top_rows(
    date_rows: list[dict[str, Any]],
    spec: WeightSpec,
    policy: SelectionPolicy,
    *,
    ret_key: str,
    top_n: int,
    params: CalibrationParams,
) -> list[dict[str, Any]]:
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for row in date_rows:
        if to_float(row.get(ret_key)) is None:
            continue
        candidate_score, scores = policy_adjusted_score(row, spec, policy, params)
        if candidate_score is None:
            continue
        candidates.append(
            (
                candidate_score,
                str(row.get("ticker") or ""),
                annotate_selected_row(row, candidate_score=candidate_score, scores=scores, policy=policy),
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in candidates[:top_n]]


def split_rows_by_completed_return_date(
    rows: list[dict[str, Any]],
    *,
    horizon: int,
    train_fraction: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    ret_key = f"fwd_{horizon}d_net_return"
    eligible_dates = sorted(
        {
            str(row.get("asof_date") or "")
            for row in rows
            if str(row.get("asof_date") or "") and to_float(row.get(ret_key)) is not None
        }
    )
    if len(eligible_dates) < 2:
        eligible_set = set(eligible_dates)
        return [row for row in rows if str(row.get("asof_date") or "") in eligible_set], [], eligible_dates, []
    bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
    split_idx = int(math.floor(len(eligible_dates) * bounded_fraction))
    split_idx = max(1, min(len(eligible_dates) - 1, split_idx))
    train_dates = eligible_dates[:split_idx]
    test_dates = eligible_dates[split_idx:]
    train_set = set(train_dates)
    test_set = set(test_dates)
    train_rows = [row for row in rows if str(row.get("asof_date") or "") in train_set]
    test_rows = [row for row in rows if str(row.get("asof_date") or "") in test_set]
    return train_rows, test_rows, train_dates, test_dates


def build_candidate_grid_row(
    rows_by_date: dict[str, list[dict[str, Any]]],
    job: CandidateGridJob,
    *,
    sample: str,
    evaluation_split: str,
    params: CalibrationParams,
) -> dict[str, Any]:
    ret_key = f"fwd_{job.horizon}d_net_return"
    selected_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    raw_baseline_rows: list[dict[str, Any]] = []
    selected_counts: list[int] = []
    date_count = 0
    for asof_date in sorted(rows_by_date):
        date_rows = rows_by_date[asof_date]
        eligible = [row for row in date_rows if to_float(row.get(ret_key)) is not None]
        if not eligible:
            continue
        selected = select_top_rows(
            eligible,
            job.spec,
            job.policy,
            ret_key=ret_key,
            top_n=job.top_n,
            params=params,
        )
        if not selected:
            continue
        policy_eligible = [
            row
            for row in eligible
            if policy_adjusted_score(row, job.spec, job.policy, params)[0] is not None
        ]
        date_count += 1
        selected_counts.append(len(selected))
        selected_rows.extend(selected)
        baseline_rows.extend(policy_eligible)
        raw_baseline_rows.extend(eligible)

    selected_summary = selection_quality_summary(selected_rows, ret_key, params=params)
    universe_summary = selection_quality_summary(baseline_rows, ret_key, params=params)
    raw_universe_summary = selection_quality_summary(raw_baseline_rows, ret_key, params=params)
    constraint_fields = calibration_constraint_fields(
        selected_summary,
        asof_dates=date_count,
        params=params,
    )
    return {
        "sample": sample,
        "evaluation_split": evaluation_split,
        "horizon_days": job.horizon,
        "horizon_unit": "trading_bars",
        "top_n": job.top_n,
        "return_basis": "net_after_round_trip_costs",
        "round_trip_cost_bps": params.round_trip_cost_bps,
        "candidate_name": job.spec.candidate_name,
        "candidate_description": job.spec.description,
        "selection_policy_name": job.policy.policy_name,
        "selection_policy_description": job.policy.description,
        "universe_baseline_type": "policy_eligible",
        "asof_dates": date_count,
        "avg_selected_names_per_date": rounded(mean([float(v) for v in selected_counts])),
        **constraint_fields,
        **spec_fields(job.spec, job.policy),
        **prefixed("selected_", selected_summary),
        **prefixed("universe_", universe_summary),
        **prefixed("raw_universe_", raw_universe_summary),
        **{
            f"selected_minus_universe_{key}": summary_metric_spread(
                selected_summary,
                universe_summary,
                key,
            )
            for key in SPREAD_KEYS
        },
    }


def build_candidate_grid_rows(
    rows: list[dict[str, Any]],
    specs: list[WeightSpec],
    policies: list[SelectionPolicy],
    horizons: list[int],
    top_ns: list[int],
    *,
    sample: str,
    evaluation_split: str,
    params: CalibrationParams,
    max_workers: int,
) -> list[dict[str, Any]]:
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)

    jobs: list[CandidateGridJob] = []
    job_index = 0
    for horizon in horizons:
        for top_n in top_ns:
            for spec in specs:
                for policy in policies:
                    jobs.append(
                        CandidateGridJob(index=job_index, horizon=horizon, top_n=top_n, spec=spec, policy=policy)
                    )
                    job_index += 1

    def worker(job: Any) -> dict[str, Any]:
        if not isinstance(job, CandidateGridJob):
            raise TypeError(f"Expected CandidateGridJob, got {type(job).__name__}")
        return build_candidate_grid_row(
            rows_by_date,
            job,
            sample=sample,
            evaluation_split=evaluation_split,
            params=params,
        )

    out = run_indexed_jobs(
        jobs,
        worker,
        max_workers=max_workers,
        job_label=f"candidate_grid:{sample}:{evaluation_split}",
    )
    attach_current_config_spreads(out, params=params)
    return out


def attach_current_config_spreads(rows: list[dict[str, Any]], *, params: CalibrationParams) -> None:
    group_keys = ["sample", "evaluation_split", "horizon_days", "top_n"]
    baseline_by_policy_group: dict[tuple[str, ...], dict[str, Any]] = {}
    raw_baseline_by_group: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("candidate_name") or "") != CURRENT_CONFIG_CANDIDATE_NAME:
            continue
        base_key = tuple(str(row.get(group_key) or "") for group_key in group_keys)
        policy_key = (*base_key, str(row.get("selection_policy_name") or ""))
        baseline_by_policy_group[policy_key] = unprefix(row, "selected_")
        if str(row.get("selection_policy_name") or "") != "raw_legacy_score":
            continue
        raw_baseline_by_group[base_key] = unprefix(row, "selected_")
    if not baseline_by_policy_group:
        LOGGER.warning("No current_config baseline rows found; current-config objective fields will be empty.")
    if not raw_baseline_by_group:
        LOGGER.warning("No raw_legacy_score current_config baseline found; raw baseline objective fields will be empty.")

    for row in rows:
        base_key = tuple(str(row.get(group_key) or "") for group_key in group_keys)
        policy_key = (*base_key, str(row.get("selection_policy_name") or ""))
        baseline = baseline_by_policy_group.get(policy_key, {})
        raw_baseline = raw_baseline_by_group.get(base_key, {})
        selected = unprefix(row, "selected_")
        row["calibration_objective_vs_current_config"] = (
            rounded(robust_objective(selected, baseline, params=params)) if baseline else ""
        )
        row["calibration_objective_vs_raw_current_config"] = (
            rounded(robust_objective(selected, raw_baseline, params=params)) if raw_baseline else ""
        )
        for spread_key in SPREAD_KEYS:
            row[f"selected_minus_current_config_{spread_key}"] = summary_metric_spread(
                selected,
                baseline,
                spread_key,
            )
            row[f"selected_minus_raw_current_config_{spread_key}"] = summary_metric_spread(
                selected,
                raw_baseline,
                spread_key,
            )


def medium_term_scope_name(medium_term_horizons: list[int]) -> str:
    return f"medium_term_{'_'.join(str(horizon) for horizon in medium_term_horizons)}"


def build_best_rows(grid_rows: list[dict[str, Any]], *, medium_term_horizons: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    group_keys = ["sample", "evaluation_split", "horizon_days", "top_n"]
    medium_horizon_labels = {str(horizon) for horizon in medium_term_horizons}
    medium_scope = medium_term_scope_name(medium_term_horizons)
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in grid_rows:
        grouped[tuple(str(row.get(key) or "") for key in group_keys)].append(row)
    for key, rows_for_group in sorted(grouped.items()):
        ranked = sorted(rows_for_group, key=calibration_sort_tuple, reverse=True)
        for rank, row in enumerate(ranked[:25], start=1):
            out.append({"scope": "horizon", "rank": rank, **{group_keys[i]: key[i] for i in range(len(group_keys))}, **row})

    medium_term_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in grid_rows:
        if str(row.get("horizon_days") or "") not in medium_horizon_labels:
            continue
        key = (
            str(row.get("sample") or ""),
            str(row.get("evaluation_split") or ""),
            str(row.get("top_n") or ""),
            str(row.get("candidate_id") or ""),
        )
        medium_term_groups[key].append(row)
    aggregate_rows: list[dict[str, Any]] = []
    for key, rows_for_candidate in medium_term_groups.items():
        objective = weighted_mean_numeric(rows_for_candidate, "calibration_objective_vs_current_config", "selected_n")
        if objective is None:
            continue
        pass_values = [1.0 if as_bool(row.get("calibration_pass")) else 0.0 for row in rows_for_candidate]
        first = rows_for_candidate[0]
        aggregate_rows.append(
            {
                "scope": medium_scope,
                "sample": key[0],
                "evaluation_split": key[1],
                "top_n": key[2],
                "candidate_id": key[3],
                "candidate_name": first.get("candidate_name", ""),
                "selection_policy_name": first.get("selection_policy_name", ""),
                "horizon_days": ",".join(str(horizon) for horizon in medium_term_horizons),
                "mean_calibration_objective_vs_current_config": rounded(objective),
                "mean_calibration_objective_vs_raw_current_config": mean_numeric(
                    rows_for_candidate,
                    "calibration_objective_vs_raw_current_config",
                ),
                "calibration_pass_rate_pct": pct(mean(pass_values)),
                "mean_selected_lcb_return_pct": mean_numeric(rows_for_candidate, "selected_lcb_return_pct"),
                "mean_selected_sortino_like": mean_numeric(rows_for_candidate, "selected_sortino_like"),
                "mean_selected_profit_factor": mean_numeric(rows_for_candidate, "selected_profit_factor"),
                "mean_selected_omega_configured": mean_numeric(rows_for_candidate, "selected_omega_configured"),
                "mean_selected_omega_0": mean_numeric(rows_for_candidate, "selected_omega_0"),
                "mean_selected_binary_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_binary_weakness_exposure_pct",
                ),
                "mean_selected_hard_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_hard_weakness_exposure_pct",
                ),
                "mean_selected_core_hard_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_core_hard_weakness_exposure_pct",
                ),
                "mean_selected_event_hard_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_event_hard_weakness_exposure_pct",
                ),
                "mean_selected_soft_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_soft_weakness_exposure_pct",
                ),
                "mean_selected_illiquid_weakness_exposure_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_illiquid_weakness_exposure_pct",
                ),
                "mean_selected_large_loss_20pct_rate_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_large_loss_20pct_rate_pct",
                ),
                "mean_selected_top3_gain_contribution_pct": mean_numeric(
                    rows_for_candidate,
                    "selected_top3_gain_contribution_pct",
                ),
                **{key_name: first.get(key_name, "") for key_name in spec_output_keys()},
            }
        )

    aggregate_grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in aggregate_rows:
        aggregate_grouped[
            (
                str(row.get("sample") or ""),
                str(row.get("evaluation_split") or ""),
                str(row.get("top_n") or ""),
            )
        ].append(row)
    for _, rows_for_group in sorted(aggregate_grouped.items()):
        ranked = sorted(
            rows_for_group,
            key=lambda row: (
                numeric_or_default(row.get("calibration_pass_rate_pct"), -1e9),
                numeric_or_default(row.get("mean_calibration_objective_vs_current_config"), -1e9),
                numeric_or_default(row.get("mean_selected_lcb_return_pct"), -1e9),
                -numeric_or_default(row.get("mean_selected_core_hard_weakness_exposure_pct"), 100.0),
            ),
            reverse=True,
        )
        for rank, row in enumerate(ranked[:25], start=1):
            out.append({"rank": rank, **row})
    return out


def build_holdout_rows(grid_rows: list[dict[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    train_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    test_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in grid_rows:
        split = str(row.get("evaluation_split") or "")
        sample = str(row.get("sample") or "")
        horizon = str(row.get("horizon_days") or "")
        top_n = str(row.get("top_n") or "")
        candidate_id = str(row.get("candidate_id") or row.get("candidate_name") or "")
        if split == "train":
            train_groups[(sample, horizon, top_n)].append(row)
        elif split == "test":
            test_by_key[(sample, horizon, top_n, candidate_id)] = row

    out: list[dict[str, Any]] = []
    metric_keys = [
        "n",
        "asof_dates",
        "calibration_pass",
        "calibration_fail_reasons",
        "calibration_objective_vs_current_config",
        "calibration_objective_vs_raw_current_config",
        "selected_mean_return_pct",
        "selected_lcb_return_pct",
        "selected_cvar_5_return_pct",
        "selected_sortino_like",
        "selected_profit_factor",
        "selected_omega_configured",
        "selected_omega_0",
        "selected_binary_weakness_exposure_pct",
        "selected_hard_weakness_exposure_pct",
        "selected_core_hard_weakness_exposure_pct",
        "selected_event_hard_weakness_exposure_pct",
        "selected_soft_weakness_exposure_pct",
        "selected_normal_binary_exposure_pct",
        "selected_illiquid_weakness_exposure_pct",
        "selected_large_loss_20pct_rate_pct",
        "selected_large_loss_40pct_rate_pct",
        "selected_top3_gain_contribution_pct",
    ]
    for (sample, horizon, top_n), rows_for_group in sorted(train_groups.items()):
        ranked = sorted(rows_for_group, key=calibration_sort_tuple, reverse=True)
        for train_rank, train_row in enumerate(ranked[: max(1, limit)], start=1):
            candidate_id = str(train_row.get("candidate_id") or train_row.get("candidate_name") or "")
            test_row = test_by_key.get((sample, horizon, top_n, candidate_id), {})
            payload: dict[str, Any] = {
                "sample": sample,
                "horizon_days": horizon,
                "horizon_unit": "trading_bars",
                "top_n": top_n,
                "train_rank": train_rank,
                "candidate_id": candidate_id,
                "candidate_name": train_row.get("candidate_name", ""),
                "candidate_description": train_row.get("candidate_description", ""),
            }
            for key in spec_output_keys():
                payload[key] = train_row.get(key, "")
            for key in metric_keys:
                train_key = "selected_n" if key == "n" else key
                payload[f"train_{key}"] = train_row.get(train_key, "")
                payload[f"test_{key}"] = test_row.get(train_key, "")
            out.append(payload)
    return out


def deterministic_bootstrap_seed(*, base_seed: int, parts: list[object]) -> int:
    text = "|".join([str(base_seed), *[str(part) for part in parts]])
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return int(digest, 16)


def selected_returns_by_date(
    rows: list[dict[str, Any]],
    spec: WeightSpec,
    policy: SelectionPolicy,
    *,
    horizon: int,
    top_n: int,
    params: CalibrationParams,
) -> list[list[float]]:
    ret_key = f"fwd_{horizon}d_net_return"
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    out: list[list[float]] = []
    for asof_date in sorted(rows_by_date):
        eligible = [row for row in rows_by_date[asof_date] if to_float(row.get(ret_key)) is not None]
        if not eligible:
            continue
        selected = select_top_rows(
            eligible,
            spec,
            policy,
            ret_key=ret_key,
            top_n=top_n,
            params=params,
        )
        returns = numeric_values(selected, ret_key)
        if returns:
            out.append(returns)
    return out


def bootstrap_metric_intervals(
    returns_by_date: list[list[float]],
    *,
    params: CalibrationParams,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    if iterations <= 0 or not returns_by_date:
        return {
            "bootstrap_iterations": 0,
            **{f"selected_{metric_key}_ci05": "" for metric_key in BOOTSTRAP_METRIC_KEYS},
            **{f"selected_{metric_key}_ci95": "" for metric_key in BOOTSTRAP_METRIC_KEYS},
        }

    rng = random.Random(seed)
    metric_values: dict[str, list[float]] = {key: [] for key in BOOTSTRAP_METRIC_KEYS}
    date_count = len(returns_by_date)
    for _ in range(iterations):
        sampled_returns: list[float] = []
        for _ in range(date_count):
            sampled_returns.extend(returns_by_date[rng.randrange(date_count)])
        summary = summarize_return_risk(sampled_returns, params=params)
        for metric_key in BOOTSTRAP_METRIC_KEYS:
            value = to_float(summary.get(metric_key))
            if value is not None:
                metric_values[metric_key].append(value)

    fields: dict[str, Any] = {"bootstrap_iterations": iterations}
    for metric_key in BOOTSTRAP_METRIC_KEYS:
        values = metric_values[metric_key]
        fields[f"selected_{metric_key}_ci05"] = rounded(quantile(values, 0.05)) if values else ""
        fields[f"selected_{metric_key}_ci95"] = rounded(quantile(values, 0.95)) if values else ""
    return fields


def build_bootstrap_ci_row(
    rows: list[dict[str, Any]],
    job: BootstrapCiJob,
    *,
    params: CalibrationParams,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    returns_by_date = selected_returns_by_date(
        rows,
        job.spec,
        job.policy,
        horizon=job.horizon,
        top_n=job.top_n,
        params=params,
    )
    returns = [value for date_returns in returns_by_date for value in date_returns]
    point_summary = summarize_return_risk(returns, params=params)
    bootstrap_seed = deterministic_bootstrap_seed(
        base_seed=seed,
        parts=[
            job.sample,
            job.evaluation_split,
            job.horizon,
            job.top_n,
            stable_candidate_id(job.spec, job.policy),
        ],
    )
    ci_fields = bootstrap_metric_intervals(
        returns_by_date,
        params=params,
        iterations=iterations,
        seed=bootstrap_seed,
    )
    return {
        "sample": job.sample,
        "evaluation_split": job.evaluation_split,
        "horizon_days": job.horizon,
        "horizon_unit": "trading_bars",
        "top_n": job.top_n,
        "train_rank": job.train_rank,
        "candidate_name": job.spec.candidate_name,
        "candidate_description": job.spec.description,
        "selection_policy_name": job.policy.policy_name,
        "selection_policy_description": job.policy.description,
        "asof_dates": len(returns_by_date),
        **spec_fields(job.spec, job.policy),
        **prefixed("selected_", point_summary),
        **ci_fields,
    }


def build_bootstrap_ci_rows(
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]],
    holdout_rows: list[dict[str, Any]],
    candidates_by_id: dict[str, tuple[WeightSpec, SelectionPolicy]],
    *,
    top_k: int,
    iterations: int,
    seed: int,
    params: CalibrationParams,
    max_workers: int,
) -> list[dict[str, Any]]:
    if iterations <= 0 or top_k <= 0:
        return []

    jobs: list[BootstrapCiJob] = []
    for row in holdout_rows:
        train_rank = int(to_float(row.get("train_rank"), 0.0) or 0)
        if train_rank <= 0 or train_rank > top_k:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        candidate_pair = candidates_by_id.get(candidate_id)
        spec = candidate_pair[0] if candidate_pair is not None else None
        policy = candidate_pair[1] if candidate_pair is not None else None
        horizon = int(to_float(row.get("horizon_days"), 0.0) or 0)
        top_n = int(to_float(row.get("top_n"), 0.0) or 0)
        sample = str(row.get("sample") or "")
        if spec is None or policy is None or horizon <= 0 or top_n <= 0 or not sample:
            continue
        for evaluation_split in ["train", "test"]:
            jobs.append(
                BootstrapCiJob(
                    index=len(jobs),
                    sample=sample,
                    evaluation_split=evaluation_split,
                    horizon=horizon,
                    top_n=top_n,
                    train_rank=train_rank,
                    spec=spec,
                    policy=policy,
                )
            )

    def worker(job: Any) -> dict[str, Any]:
        if not isinstance(job, BootstrapCiJob):
            raise TypeError(f"Expected BootstrapCiJob, got {type(job).__name__}")
        rows = split_rows_by_key.get((job.sample, job.evaluation_split, job.horizon), [])
        return build_bootstrap_ci_row(
            rows,
            job,
            params=params,
            iterations=iterations,
            seed=seed,
        )

    return run_indexed_jobs(
        jobs,
        worker,
        max_workers=max_workers,
        job_label="bootstrap_ci",
    )


def selected_rows_by_date(
    rows: list[dict[str, Any]],
    spec: WeightSpec,
    policy: SelectionPolicy,
    *,
    horizon: int,
    top_n: int,
    params: CalibrationParams,
) -> list[dict[str, Any]]:
    ret_key = f"fwd_{horizon}d_net_return"
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    out: list[dict[str, Any]] = []
    for asof_date in sorted(rows_by_date):
        eligible = [row for row in rows_by_date[asof_date] if to_float(row.get(ret_key)) is not None]
        if not eligible:
            continue
        selected = select_top_rows(
            eligible,
            spec,
            policy,
            ret_key=ret_key,
            top_n=top_n,
            params=params,
        )
        for rank, selected_row in enumerate(selected, start=1):
            out.append({"selected_rank_within_date": rank, **selected_row})
    return out


def selected_ticker_diagnostic_record(
    row: dict[str, Any],
    *,
    sample: str,
    evaluation_split: str,
    horizon: int,
    top_n: int,
    train_rank: int,
    spec: WeightSpec,
    policy: SelectionPolicy,
) -> dict[str, Any]:
    ret_key = f"fwd_{horizon}d_net_return"
    gross_key = f"fwd_{horizon}d_return"
    return {
        "sample": sample,
        "evaluation_split": evaluation_split,
        "horizon_days": horizon,
        "horizon_unit": "trading_bars",
        "top_n": top_n,
        "train_rank": train_rank,
        "candidate_id": stable_candidate_id(spec, policy),
        "candidate_name": spec.candidate_name,
        "selection_policy_name": policy.policy_name,
        "asof_date": row.get("asof_date", ""),
        "selected_rank_within_date": row.get("selected_rank_within_date", ""),
        "ticker": row.get("ticker", ""),
        "company_name": row.get("company_name", ""),
        "profile_name": row.get("profile_name", ""),
        "candidate_selection_score": row.get("candidate_selection_score", ""),
        "candidate_pre_confidence_opportunity_score": row.get("candidate_pre_confidence_opportunity_score", ""),
        "candidate_investment_score": row.get("candidate_investment_score", ""),
        "risk_score_raw": row.get("risk_score_raw", ""),
        "net_forward_return_pct": pct(to_float(row.get(ret_key))),
        "gross_forward_return_pct": pct(to_float(row.get(gross_key))),
        "entry_date": row.get(f"fwd_{horizon}d_entry_date", ""),
        "target_date": row.get(f"fwd_{horizon}d_target_date", ""),
        "binary_weakness_severity": row.get("diag_binary_weakness_severity", ""),
        "binary_weakness_reasons": row.get("diag_binary_weakness_reasons", ""),
        "hard_weakness_flag": row.get("diag_hard_weakness_flag", ""),
        "hard_weakness_reasons": row.get("diag_hard_weakness_reasons", ""),
        "core_hard_weakness_flag": row.get("diag_core_hard_weakness_flag", ""),
        "core_hard_weakness_reasons": row.get("diag_core_hard_weakness_reasons", ""),
        "event_hard_weakness_flag": row.get("diag_event_hard_weakness_flag", ""),
        "event_hard_weakness_reasons": row.get("diag_event_hard_weakness_reasons", ""),
        "soft_weakness_flag": row.get("diag_soft_weakness_flag", ""),
        "soft_weakness_reasons": row.get("diag_soft_weakness_reasons", ""),
        "normal_clinical_binary_flag": row.get("diag_normal_clinical_binary_flag", ""),
        "illiquid_weakness_flag": row.get("diag_illiquid_weakness_flag", ""),
        "avg_dollar_volume_20d": row.get("diag_avg_dollar_volume_20d", ""),
        "liquidity_ok": row.get("diag_liquidity_ok", ""),
        "cash_runway_months": row.get("diag_cash_runway_months", ""),
        "verified_active_trial_count": row.get("diag_verified_active_trial_count", ""),
        "has_advanced_trial_anchor": row.get("diag_has_advanced_trial_anchor", ""),
        "has_business_anchor": row.get("diag_has_business_anchor", ""),
        "commercial_fragility_risk_score": row.get("diag_commercial_fragility_risk_score", ""),
        "raw_score_missing_flag": row.get("diag_raw_score_missing_flag", ""),
        "raw_score_missing_count": row.get("diag_raw_score_missing_count", ""),
        "raw_score_missing_fields": row.get("diag_raw_score_missing_fields", ""),
    }


def build_selected_ticker_diagnostic_rows(
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]],
    holdout_rows: list[dict[str, Any]],
    candidates_by_id: dict[str, tuple[WeightSpec, SelectionPolicy]],
    *,
    top_train_ranks: int,
    params: CalibrationParams,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int, int, str]] = set()
    for holdout in holdout_rows:
        train_rank = int(to_float(holdout.get("train_rank"), 0.0) or 0)
        if train_rank <= 0 or train_rank > max(1, int(top_train_ranks)):
            continue
        sample = str(holdout.get("sample") or "")
        horizon = int(to_float(holdout.get("horizon_days"), 0.0) or 0)
        top_n = int(to_float(holdout.get("top_n"), 0.0) or 0)
        candidate_id = str(holdout.get("candidate_id") or "")
        candidate_pair = candidates_by_id.get(candidate_id)
        if not sample or horizon <= 0 or top_n <= 0 or candidate_pair is None:
            continue
        spec, policy = candidate_pair
        for evaluation_split in ["train", "test"]:
            key = (sample, evaluation_split, horizon, top_n, train_rank, candidate_id)
            if key in seen:
                continue
            seen.add(key)
            rows = split_rows_by_key.get((sample, evaluation_split, horizon), [])
            selected = selected_rows_by_date(
                rows,
                spec,
                policy,
                horizon=horizon,
                top_n=top_n,
                params=params,
            )
            for selected_row in selected:
                out.append(
                    selected_ticker_diagnostic_record(
                        selected_row,
                        sample=sample,
                        evaluation_split=evaluation_split,
                        horizon=horizon,
                        top_n=top_n,
                        train_rank=train_rank,
                        spec=spec,
                        policy=policy,
                    )
                )
    return out


def split_reason_tokens(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def build_binary_weakness_component_rows(selected_diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = defaultdict(
        lambda: {"selected_n": 0, "reason_counts": defaultdict(int)}
    )
    group_keys = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "train_rank",
        "candidate_id",
        "candidate_name",
        "selection_policy_name",
    ]
    for row in selected_diagnostics:
        key = tuple(str(row.get(k) or "") for k in group_keys)
        grouped[key]["selected_n"] += 1
        for severity, reason_key in [
            ("core_hard", "core_hard_weakness_reasons"),
            ("event_hard", "event_hard_weakness_reasons"),
            ("soft", "soft_weakness_reasons"),
        ]:
            for reason in split_reason_tokens(row.get(reason_key)):
                grouped[key]["reason_counts"][(severity, reason)] += 1
        core_or_event_reasons = [
            *split_reason_tokens(row.get("core_hard_weakness_reasons")),
            *split_reason_tokens(row.get("event_hard_weakness_reasons")),
        ]
        if to_float(row.get("normal_clinical_binary_flag"), 0.0) and not core_or_event_reasons:
            grouped[key]["reason_counts"][("normal", "normal_clinical_binary_without_hard_weakness")] += 1

    out: list[dict[str, Any]] = []
    for key, payload in sorted(grouped.items()):
        selected_n = int(payload["selected_n"])
        reason_counts = payload["reason_counts"]
        if not isinstance(reason_counts, defaultdict):
            raise TypeError("Expected reason_counts to be a defaultdict")
        for (severity, reason), count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
            row: dict[str, Any] = {group_keys[i]: key[i] for i in range(len(group_keys))}
            row.update(
                {
                    "weakness_severity": severity,
                    "weakness_reason": reason,
                    "reason_count": count,
                    "selected_n": selected_n,
                    "reason_exposure_pct": round(100.0 * count / selected_n, 6) if selected_n else "",
                }
            )
            out.append(row)
    return out


def build_binary_weakness_severity_rows(
    selected_diagnostics: list[dict[str, Any]],
    *,
    params: CalibrationParams,
) -> list[dict[str, Any]]:
    group_keys = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "train_rank",
        "candidate_id",
        "candidate_name",
        "selection_policy_name",
        "binary_weakness_severity",
    ]
    grouped: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for row in selected_diagnostics:
        ret = to_float(row.get("net_forward_return_pct"))
        if ret is None:
            continue
        key = tuple(str(row.get(k) or "") for k in group_keys)
        grouped[key].append(ret / 100.0)
    out: list[dict[str, Any]] = []
    for key, returns in sorted(grouped.items()):
        row: dict[str, Any] = {group_keys[i]: key[i] for i in range(len(group_keys))}
        row.update(summarize_return_risk(returns, params=params))
        out.append(row)
    return out


def liquidity_ok_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if to_float(row.get("diag_liquidity_ok")) == 1.0]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    configure_logging()
    args = parse_args()
    start_time = time.perf_counter()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "calibration.tier1.output_dir", "../output/biotech_index_reports/calibration_tier1_120d_confirm"),
            base_dir=base_dir,
        )
    )
    start_asof = parse_date(args.start_asof)
    end_asof = parse_date(args.end_asof)
    if args.start_asof and start_asof is None:
        raise ValueError(f"Invalid --start-asof date: {args.start_asof}")
    if args.end_asof and end_asof is None:
        raise ValueError(f"Invalid --end-asof date: {args.end_asof}")
    horizons = parse_int_list(args.horizons, default=[20, 60, 120])
    top_ns = parse_int_list(args.top_n, default=[10, 20, 30])
    market_sources = [
        token.strip()
        for raw_source in normalize_string_list(args.market_sources, ["yahoo_adjusted", "interactive_brokers"])
        for token in str(raw_source).split(",")
        if token.strip()
    ]
    extra_exclusions = parse_string_set(args.exclude_tickers) | parse_string_set(
        cfg_get(config, "calibration.exclude_tickers", [])
    )
    exclude_current_removals = (
        args.exclude_current_removals
        if args.exclude_current_removals is not None
        else as_bool(cfg_get(config, "calibration.tier1.exclude_current_removals", False), False)
    )
    strict_feature_lag = (
        args.strict_feature_lag
        if args.strict_feature_lag is not None
        else as_bool(cfg_get(config, "calibration.tier1.strict_feature_lag", True), True)
    )
    next_bar_entry = (
        args.next_bar_entry
        if args.next_bar_entry is not None
        else as_bool(cfg_get(config, "calibration.tier1.next_bar_entry", True), True)
    )
    try:
        train_fraction = (
            float(args.train_fraction)
            if args.train_fraction is not None
            else float(cfg_get(config, "calibration.tier1.train_fraction", 0.70))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("calibration.tier1.train_fraction must be numeric") from exc
    if not 0.10 <= train_fraction <= 0.90:
        raise ValueError(f"--train-fraction must be between 0.10 and 0.90, got {train_fraction}")
    max_workers = (
        int(args.max_workers)
        if args.max_workers is not None
        else int(cfg_get(config, "calibration.tier1.max_workers", os.cpu_count() or 1))
    )
    max_workers = max(1, min(max_workers, (os.cpu_count() or 1) * 4))
    bootstrap_iterations = (
        int(args.bootstrap_iterations)
        if args.bootstrap_iterations is not None
        else int(cfg_get(config, "calibration.tier1.bootstrap_iterations", DEFAULT_BOOTSTRAP_ITERATIONS))
    )
    if bootstrap_iterations < 0:
        raise ValueError(f"--bootstrap-iterations must be >= 0, got {bootstrap_iterations}")
    bootstrap_top_k = (
        int(args.bootstrap_top_k)
        if args.bootstrap_top_k is not None
        else int(cfg_get(config, "calibration.tier1.bootstrap_top_k", DEFAULT_BOOTSTRAP_TOP_K))
    )
    bootstrap_top_k = max(0, bootstrap_top_k)
    holdout_top_k = (
        int(args.holdout_top_k)
        if args.holdout_top_k is not None
        else int(cfg_get(config, "calibration.tier1.holdout_top_k", DEFAULT_HOLDOUT_TOP_K))
    )
    holdout_top_k = max(1, holdout_top_k)
    bootstrap_seed = (
        int(args.bootstrap_seed)
        if args.bootstrap_seed is not None
        else int(cfg_get(config, "calibration.tier1.bootstrap_seed", DEFAULT_BOOTSTRAP_SEED))
    )
    medium_term_horizons_raw = args.medium_term_horizons or ",".join(
        normalize_string_list(cfg_get(config, "calibration.tier1.medium_term_horizons", ["60", "120"]), ["60", "120"])
    )
    medium_term_horizons = parse_int_list(medium_term_horizons_raw, default=[60, 120])
    missing_medium_horizons = sorted(set(medium_term_horizons) - set(horizons))
    if missing_medium_horizons:
        LOGGER.warning(
            "Configured medium-term horizons are not present in --horizons and will be skipped: %s",
            missing_medium_horizons,
        )
    active_medium_horizons = sorted(set(medium_term_horizons) & set(horizons))
    if len(active_medium_horizons) == 1:
        LOGGER.warning(
            "Only one configured medium-term horizon is active; medium-term aggregate will match a single horizon: %s",
            active_medium_horizons,
        )
    params = load_calibration_params(config)
    specs = generate_weight_specs(config, candidate_limit=max(0, int(args.candidate_limit)))
    policies = generate_selection_policies(config)
    candidates_by_id = {stable_candidate_id(spec, policy): (spec, policy) for spec in specs for policy in policies}
    selected_diagnostic_top_ranks = max(
        1,
        int(
            args.selected_ticker_diagnostic_top_ranks
            if args.selected_ticker_diagnostic_top_ranks is not None
            else cfg_get(
                config,
                "calibration.tier1.selected_ticker_diagnostic_top_ranks",
                DEFAULT_SELECTED_TICKER_DIAGNOSTIC_TOP_RANKS,
            )
        ),
    )
    min_addv20 = float(cfg_get(config, "multibagger.min_addv20", 1_000_000.0))

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = load_snapshot_dates(
            conn,
            start_asof=start_asof,
            end_asof=end_asof,
            fridays_only=not args.include_non_fridays,
            max_snapshots=max(0, int(args.max_snapshots)),
        )
        if not snapshot_dates:
            raise ValueError("No daily_features snapshot dates found for Tier-1 calibration.")
        excluded_tickers = load_excluded_tickers(
            conn,
            exclude_current_removals=exclude_current_removals,
            extra=extra_exclusions,
        )
        observations = load_observations(
            conn,
            snapshot_dates,
            excluded_tickers,
            config,
            min_addv20=min_addv20,
            strict_feature_lag=strict_feature_lag,
        )
        if not observations:
            raise ValueError("No Tier-1 feature observations remain after exclusions.")
        tickers = {ticker for row in observations if (ticker := normalize_ticker(row["ticker"]))}
        asof_dates = [parsed for row in observations if (parsed := parse_date(row["asof_date"])) is not None]
        if not asof_dates:
            raise ValueError("Tier-1 feature observations do not contain valid as-of dates.")
        bars_by_ticker = load_bars(conn, tickers=tickers, min_date=min(asof_dates), market_sources=market_sources)

    add_forward_returns(
        observations,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
    )
    candidate_rows: list[dict[str, Any]] = []
    split_manifest: dict[str, Any] = {}
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for horizon in horizons:
        train_observations, test_observations, train_dates, test_dates = split_rows_by_completed_return_date(
            observations,
            horizon=horizon,
            train_fraction=train_fraction,
        )
        liquid_train_observations = liquidity_ok_rows(train_observations)
        liquid_test_observations = liquidity_ok_rows(test_observations)
        split_rows_by_key[("all", "train", horizon)] = train_observations
        split_rows_by_key[("all", "test", horizon)] = test_observations
        split_rows_by_key[("liquidity_ok", "train", horizon)] = liquid_train_observations
        split_rows_by_key[("liquidity_ok", "test", horizon)] = liquid_test_observations
        split_manifest[str(horizon)] = {
            "train_snapshot_dates": train_dates,
            "train_snapshot_date_count": len(train_dates),
            "test_snapshot_dates": test_dates,
            "test_snapshot_date_count": len(test_dates),
            "train_observation_count": len(train_observations),
            "test_observation_count": len(test_observations),
            "train_liquidity_ok_observation_count": len(liquid_train_observations),
            "test_liquidity_ok_observation_count": len(liquid_test_observations),
        }
        candidate_rows.extend(
            build_candidate_grid_rows(
                train_observations,
                specs,
                policies,
                [horizon],
                top_ns,
                sample="all",
                evaluation_split="train",
                params=params,
                max_workers=max_workers,
            )
        )
        candidate_rows.extend(
            build_candidate_grid_rows(
                liquid_train_observations,
                specs,
                policies,
                [horizon],
                top_ns,
                sample="liquidity_ok",
                evaluation_split="train",
                params=params,
                max_workers=max_workers,
            )
        )
        candidate_rows.extend(
            build_candidate_grid_rows(
                test_observations,
                specs,
                policies,
                [horizon],
                top_ns,
                sample="all",
                evaluation_split="test",
                params=params,
                max_workers=max_workers,
            )
        )
        candidate_rows.extend(
            build_candidate_grid_rows(
                liquid_test_observations,
                specs,
                policies,
                [horizon],
                top_ns,
                sample="liquidity_ok",
                evaluation_split="test",
                params=params,
                max_workers=max_workers,
            )
    )
    best_rows = build_best_rows(candidate_rows, medium_term_horizons=medium_term_horizons)
    holdout_rows = build_holdout_rows(candidate_rows, limit=holdout_top_k)
    bootstrap_ci_rows = build_bootstrap_ci_rows(
        split_rows_by_key,
        holdout_rows,
        candidates_by_id,
        top_k=bootstrap_top_k,
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
        params=params,
        max_workers=max_workers,
    )
    spec_rows = [spec_fields(spec) for spec in specs]
    policy_rows = [policy_fields(policy) for policy in policies]
    candidate_policy_rows = [spec_fields(spec, policy) for spec in specs for policy in policies]
    selected_ticker_diagnostic_rows = build_selected_ticker_diagnostic_rows(
        split_rows_by_key,
        holdout_rows,
        candidates_by_id,
        top_train_ranks=selected_diagnostic_top_ranks,
        params=params,
    )
    binary_weakness_component_rows = build_binary_weakness_component_rows(selected_ticker_diagnostic_rows)
    binary_weakness_severity_rows = build_binary_weakness_severity_rows(
        selected_ticker_diagnostic_rows,
        params=params,
    )

    write_csv(output_dir / "tier1_weight_candidate_specs.csv", spec_rows)
    write_csv(output_dir / "tier1_selection_policy_specs.csv", policy_rows)
    write_csv(output_dir / "tier1_candidate_policy_specs.csv", candidate_policy_rows)
    write_csv(output_dir / "tier1_weight_calibration_grid.csv", candidate_rows)
    write_csv(output_dir / "tier1_weight_calibration_best.csv", best_rows)
    write_csv(output_dir / "tier1_weight_calibration_holdout.csv", holdout_rows)
    write_csv(output_dir / "tier1_weight_calibration_bootstrap_ci.csv", bootstrap_ci_rows)
    write_csv(output_dir / "tier1_selected_ticker_diagnostics.csv", selected_ticker_diagnostic_rows)
    write_csv(output_dir / "tier1_binary_weakness_components.csv", binary_weakness_component_rows)
    write_csv(output_dir / "tier1_binary_weakness_severity.csv", binary_weakness_severity_rows)

    horizon_counts = {
        str(horizon): sum(1 for row in observations if to_float(row.get(f"fwd_{horizon}d_return")) is not None)
        for horizon in horizons
    }
    best_medium_term = [
        row
        for row in best_rows
        if str(row.get("scope") or "") == medium_term_scope_name(medium_term_horizons)
        and str(row.get("rank") or "") == "1"
    ]
    manifest = {
        "script": Path(__file__).name,
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "snapshot_dates": snapshot_dates,
        "snapshot_date_count": len(snapshot_dates),
        "train_fraction": train_fraction,
        "horizon_split_details": split_manifest,
        "observation_count_after_exclusions": len(observations),
        "ticker_count_after_exclusions": len(tickers),
        "excluded_ticker_count": len(excluded_tickers),
        "excluded_tickers_sample": sorted(excluded_tickers)[:100],
        "market_sources": market_sources,
        "horizons": horizons,
        "top_n": top_ns,
        "weight_spec_count": len(specs),
        "selection_policy_count": len(policies),
        "candidate_count": len(candidates_by_id),
        "selected_ticker_diagnostic_top_ranks": selected_diagnostic_top_ranks,
        "selected_ticker_diagnostic_row_count": len(selected_ticker_diagnostic_rows),
        "max_workers": max_workers,
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_top_k": bootstrap_top_k,
        "holdout_top_k": holdout_top_k,
        "bootstrap_seed": bootstrap_seed,
        "bootstrap_ci_row_count": len(bootstrap_ci_rows),
        "forward_return_observation_counts": horizon_counts,
        "exclude_current_removals": exclude_current_removals,
        "strict_feature_lag": strict_feature_lag,
        "next_bar_entry": next_bar_entry,
        "medium_term_horizons": medium_term_horizons,
        "calibration_params": {
            "round_trip_cost_bps": params.round_trip_cost_bps,
            "lcb_z": params.lcb_z,
            "cvar_q": params.cvar_q,
            "omega_hurdle": params.omega_hurdle,
            "min_selected_observations": params.min_selected_observations,
            "min_asof_dates": params.min_asof_dates,
            "min_net_lcb_return_pct": params.min_net_lcb_return_pct,
            "min_sortino": params.min_sortino,
            "min_profit_factor": params.min_profit_factor,
            "min_omega": params.min_omega,
            "omega_constraint_active": abs(float(params.omega_hurdle)) > 1e-12,
            "max_binary_weakness_exposure_pct": params.max_binary_weakness_exposure_pct,
            "max_hard_weakness_exposure_pct": params.max_hard_weakness_exposure_pct,
            "max_core_hard_weakness_exposure_pct": params.max_core_hard_weakness_exposure_pct,
            "max_illiquid_weakness_exposure_pct": params.max_illiquid_weakness_exposure_pct,
            "legacy_binary_constraint_enabled": params.legacy_binary_constraint_enabled,
            "aggregate_hard_constraint_enabled": params.aggregate_hard_constraint_enabled,
            "max_top3_gain_contribution_pct": params.max_top3_gain_contribution_pct,
            "max_large_loss_20_rate_pct": params.max_large_loss_20_rate_pct,
            "max_large_loss_40_rate_pct": params.max_large_loss_40_rate_pct,
            "convex_risk_penalty_enabled": params.convex_risk_penalty_enabled,
            "risk_penalty_convexity": params.risk_penalty_convexity,
            "risk_penalty_inflection": params.risk_penalty_inflection,
        },
        "best_medium_term_rank1": best_medium_term,
        "elapsed_sec": round(time.perf_counter() - start_time, 3),
        "notes": [
            "This script calibrates only bottom-up biotech_opportunity_score weights.",
            "It recomputes candidate Tier-1 scores from historical daily_features plus prior commercial, guidance, and governance feature rows by default.",
            "It does not tune multibagger, second-stage, speculative-alpha, or anchored-overlay parameters.",
            "Candidates are ranked against the same-policy current_config using net returns after configured round-trip costs.",
            "calibration_objective_vs_raw_current_config remains available for comparison against the raw_legacy_score current_config baseline.",
            "Candidate selection is reported on chronological train and test splits; use tier1_weight_calibration_holdout.csv to compare in-sample winners against out-of-sample results.",
            "tier1_weight_calibration_bootstrap_ci.csv reports date-cluster bootstrap 5th/95th percentile intervals for top train-ranked candidates.",
            "Candidate-grid and bootstrap jobs can run in parallel via --max-workers or calibration.tier1.max_workers.",
            "The horizon_days column is retained for compatibility but represents trading bars, not calendar days.",
            "Pass/fail constraints use net LCB return, Sortino, profit factor, Omega, core hard-weakness exposure, illiquid exposure, large-loss rates, and top-winner concentration.",
            "omega_configured is reported for every run; its objective weight and constraint are active only when omega_hurdle is non-zero so it is not a duplicate of profit factor at the default hurdle.",
            "The broad legacy binary weakness flag is advisory by default; calibration separates core structural hard weakness, event/dilution hard weakness, soft weakness, and normal clinical-stage binary exposure.",
            "Aggregate hard-weakness exposure is advisory by default because event/dilution reasons can behave differently from structural non-investability; enable aggregate_hard_constraint_enabled to enforce it.",
            "tier1_selected_ticker_diagnostics.csv lists selected tickers by date for the top train-ranked candidates and includes exact hard/soft weakness reasons.",
            "tier1_binary_weakness_components.csv aggregates weakness reasons so scoring changes can target the true failure modes.",
            "Universe and selected summaries treat each ticker/date observation as a panel observation; repeat tickers on different dates are intentionally counted separately.",
            "Train/test splits are computed per horizon using dates with completed forward returns; compare cross-horizon results with the horizon_split_details manifest section.",
            "Financial quality intentionally affects both the clinical subscore and the profile-level investment score; review both weight layers when interpreting sensitivity.",
            "Current removals/manual exclusions are not excluded by default to reduce survivorship bias; set --exclude-current-removals to match current investable-universe diagnostics.",
        ],
    }
    write_json(output_dir / "tier1_weight_calibration_manifest.json", manifest)
    LOGGER.info(
        "Tier-1 weight calibration written: output_dir=%s rows=%d candidates=%d horizon_counts=%s elapsed=%.3fs",
        output_dir,
        len(observations),
        len(candidates_by_id),
        horizon_counts,
        time.perf_counter() - start_time,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise
        LOGGER.exception("Unhandled exception in main()")
        sys.exit(1)
